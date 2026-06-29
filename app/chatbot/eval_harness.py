"""Offline evaluation harness for the Sleuth assistant.

Six pure, network-free metrics. The model and agent tools are injected as
callables (like ``evals.judge`` and ``agent.run_agent``), so everything runs in
CI without a live provider and doubles as a reusable measurement toolkit:

  - llm_judge   : grounding / faithfulness score          -> app.chatbot.evals
  - trajectory  : did the read-only agent take a sensible tool path?
  - outcome     : did the final Response achieve the goal?
  - confidence  : is the judge's score calibrated? (Brier score)
  - reliability : are answers / routes stable across reruns?
  - pass_at_k   : fraction of K samples that pass

Nothing here touches a network or database. The two pieces that would normally
need DB/LLM access (trajectory over ``agent.run_agent``, outcome over a
caller-supplied Response) receive their dependencies through injected callables,
so the same code serves unit tests, offline golden-set batches, and live smoke
runs. The live counterpart is ``cloud_llm.metrics_snapshot``; these metrics are
designed to corroborate it.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence


# ---------------------------------------------------------------------------
# Trajectory — score the read-only agent's tool-use path.
# ---------------------------------------------------------------------------
_TOOL_ACTIONS = frozenset({"query", "retrieve"})
_TERMINAL_ACTIONS = frozenset({"final", "answer_data"})
_KNOWN_ACTIONS = _TOOL_ACTIONS | _TERMINAL_ACTIONS


@dataclass
class TrajectoryScore:
    """How well an agent run navigated its tools to an answer."""
    valid: bool          # all known actions, tools-then-terminal ordering
    efficient: bool      # finished within the step budget
    grounded_final: bool # final answer backed by retrieved data
    steps: int
    score: float         # blended 0..1 quality signal

    @property
    def ok(self) -> bool:
        """Well-formed trajectory with a grounded final answer."""
        return self.valid and self.grounded_final


def score_trajectory(actions: Sequence[str], *, max_steps: int,
                     grounded_final: bool) -> TrajectoryScore:
    """Score an agent's ordered tool-use trajectory.

    ``actions`` is the sequence the loop emitted, e.g.
    ``["retrieve", "query", "final"]``. Valid means every step is a known
    action, the last step is terminal (``final`` / ``answer_data``), and no
    terminal appears mid-run. ``efficient`` means it finished within
    ``max_steps``. ``grounded_final`` is decided by the caller (a ``final``
    that cited retrieved records, or an ``answer_data`` with a real table) and
    lifts the blended score.
    """
    acts = [str(a).strip().lower() for a in actions]
    steps = len(acts)
    valid = (
        bool(acts)
        and all(a in _KNOWN_ACTIONS for a in acts)
        and acts[-1] in _TERMINAL_ACTIONS
        and all(a in _TOOL_ACTIONS for a in acts[:-1])
    )
    efficient = 0 < steps <= max(1, max_steps)
    score = 0.0
    if valid:
        # Validity is the floor; grounding and efficiency each add weight.
        score = 0.5 + (0.3 if grounded_final else 0.0) + (0.2 if efficient else 0.0)
    return TrajectoryScore(valid=valid, efficient=efficient,
                           grounded_final=grounded_final, steps=steps,
                           score=round(score, 3))


def run_and_trace(message: str, *, call_model: Callable, run_query: Callable,
                  run_retrieve: Callable, max_steps: int,
                  history: str = "", context: str = "") -> "tuple[Any, list[str]]":
    """Run ``agent.run_agent`` while recording the action sequence.

    Returns ``(AgentResult, actions)`` where ``actions`` ends with the terminal
    action (``final`` / ``answer_data``), or stops at the last tool call if the
    loop gave up (kind == "none"). Model and tools are injected, so a scripted
    ``call_model`` makes the whole run deterministic.
    """
    from app.chatbot import agent
    actions: list[str] = []

    def traced_query(canonical: str) -> str:
        actions.append("query")
        return run_query(canonical)

    def traced_retrieve(query: str):
        actions.append("retrieve")
        return run_retrieve(query)

    result = agent.run_agent(
        message, call_model=call_model, run_query=traced_query,
        run_retrieve=traced_retrieve, max_steps=max_steps,
        history=history, context=context,
    )
    if result.kind == "data":
        actions.append("answer_data")
    elif result.kind == "text":
        actions.append("final")
    return result, actions


# ---------------------------------------------------------------------------
# Outcome — did the final Response achieve the goal?
# ---------------------------------------------------------------------------
@dataclass
class OutcomeResult:
    """Whether a Response matched an expected-goal spec."""
    ok: bool
    reasons: list[str] = field(default_factory=list)


def check_outcome(resp: Any, spec: dict) -> OutcomeResult:
    """Compare a Sleuth ``Response`` against an expected-goal ``spec``.

    Recognised spec keys (all optional): ``intent`` (exact match),
    ``intent_prefix`` (startswith), ``has_table`` / ``has_file`` (a block of
    that kind is present), ``text_contains`` (case-insensitive substring across
    all text blocks).
    """
    if resp is None:
        return OutcomeResult(ok=False, reasons=["no response"])
    reasons: list[str] = []
    intent = getattr(resp, "intent", "") or ""
    blocks = getattr(resp, "blocks", []) or []
    kinds = [getattr(b, "kind", "") for b in blocks]

    if "intent" in spec and intent != spec["intent"]:
        reasons.append(f"intent {intent!r} != {spec['intent']!r}")
    if "intent_prefix" in spec and not intent.startswith(spec["intent_prefix"]):
        reasons.append(f"intent {intent!r} lacks prefix {spec['intent_prefix']!r}")
    if spec.get("has_table") and "table" not in kinds:
        reasons.append("expected a table block")
    if spec.get("has_file") and "file" not in kinds:
        reasons.append("expected a file block")
    if "text_contains" in spec:
        joined = " ".join(
            str(getattr(b, "payload", {}).get("text", ""))
            for b in blocks if getattr(b, "kind", "") == "text"
        ).lower()
        if spec["text_contains"].lower() not in joined:
            reasons.append(f"text missing {spec['text_contains']!r}")
    return OutcomeResult(ok=not reasons, reasons=reasons)


# ---------------------------------------------------------------------------
# Confidence — is the judge's 0..1 score calibrated against actual correctness?
# ---------------------------------------------------------------------------
def brier_score(pairs: Sequence["tuple[float, bool]"]) -> float:
    """Mean squared error between predicted confidence (0..1) and the true
    binary outcome. 0.0 is perfect calibration; 1.0 is worst. Empty -> 0.0."""
    items = list(pairs)
    if not items:
        return 0.0
    total = sum((float(p) - (1.0 if y else 0.0)) ** 2 for p, y in items)
    return round(total / len(items), 4)


# ---------------------------------------------------------------------------
# Reliability — stability of answers / routes across reruns.
# ---------------------------------------------------------------------------
def answer_variance(answers: Sequence[str]) -> float:
    """Fraction of distinct whitespace/case-normalised replies across reruns.
    0.0 = identical every time, 1.0 = all different. Deterministic data routes
    should score near 0; conversational replies can vary. Empty -> 0.0."""
    norm = [" ".join((a or "").lower().split()) for a in answers]
    return round(len(set(norm)) / len(norm), 4) if norm else 0.0


def route_stability(routes: Sequence[Any]) -> float:
    """Fraction of reruns that match the most common route. 1.0 = perfectly
    stable. Empty -> 1.0."""
    items = list(routes)
    if not items:
        return 1.0
    most = Counter(items).most_common(1)[0][1]
    return round(most / len(items), 4)


def self_consistency(values: Sequence[Any]) -> float:
    """Fraction of a repeated discrete signal that equals the modal value
    (e.g. a judge verdict re-run N times). 1.0 = identical every time."""
    return route_stability(values)


# ---------------------------------------------------------------------------
# Agreement — judge decision vs a hand-labelled golden set.
# ---------------------------------------------------------------------------
def agreement(predicted: Sequence[bool], expected: Sequence[bool]) -> float:
    """Fraction of cases where ``predicted`` matches ``expected`` (zipped to
    the shorter sequence). Empty -> 1.0."""
    pairs = list(zip(predicted, expected))
    if not pairs:
        return 1.0
    return round(sum(1 for p, e in pairs if p == e) / len(pairs), 4)


# ---------------------------------------------------------------------------
# pass@k — fraction of tasks where at least one of K attempts passed.
# ---------------------------------------------------------------------------
def pass_at_k(samples: Sequence[bool]) -> float:
    """Per-task pass@k: 1.0 if ANY of the K attempts passed, else 0.0."""
    return 1.0 if any(samples) else 0.0


def aggregate_pass_at_k(per_task: Sequence[Sequence[bool]]) -> float:
    """Mean pass@k across all tasks, in [0, 1]. Empty -> 0.0."""
    tasks = list(per_task)
    if not tasks:
        return 0.0
    return round(sum(pass_at_k(s) for s in tasks) / len(tasks), 4)


__all__ = [
    "TrajectoryScore", "score_trajectory", "run_and_trace",
    "OutcomeResult", "check_outcome",
    "brier_score",
    "answer_variance", "route_stability", "self_consistency",
    "agreement",
    "pass_at_k", "aggregate_pass_at_k",
]
