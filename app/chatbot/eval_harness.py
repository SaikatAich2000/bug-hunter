"""Offline eval harness: pure, network-free metrics (trajectory, outcome,
calibration, reliability, agreement, pass@k). Model/tools are injected so
everything runs in CI; live counterpart is cloud_llm.metrics_snapshot.
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
    """Score an ordered action sequence: valid = known actions, terminal only
    last; efficient = within max_steps; grounded_final is caller-decided."""
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
        # validity is the floor; grounding and efficiency add weight
        score = 0.5 + (0.3 if grounded_final else 0.0) + (0.2 if efficient else 0.0)
    return TrajectoryScore(valid=valid, efficient=efficient,
                           grounded_final=grounded_final, steps=steps,
                           score=round(score, 3))


def run_and_trace(message: str, *, call_model: Callable, run_query: Callable,
                  run_retrieve: Callable, max_steps: int,
                  history: str = "", context: str = "") -> "tuple[Any, list[str]]":
    """Run ``agent.run_agent`` recording the action sequence; returns (result, actions)."""
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
    """Compare a Response against a spec (intent, intent_prefix, has_table,
    has_file, text_contains — all optional)."""
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
    """MSE of confidence vs binary outcome (0.0 = perfect). Empty -> 0.0."""
    items = list(pairs)
    if not items:
        return 0.0
    total = sum((float(p) - (1.0 if y else 0.0)) ** 2 for p, y in items)
    return round(total / len(items), 4)


# ---------------------------------------------------------------------------
# Reliability — stability of answers / routes across reruns.
# ---------------------------------------------------------------------------
def answer_variance(answers: Sequence[str]) -> float:
    """Fraction of distinct normalised replies across reruns (0.0 = identical)."""
    norm = [" ".join((a or "").lower().split()) for a in answers]
    return round(len(set(norm)) / len(norm), 4) if norm else 0.0


def route_stability(routes: Sequence[Any]) -> float:
    """Fraction of reruns matching the modal route (1.0 = stable). Empty -> 1.0."""
    items = list(routes)
    if not items:
        return 1.0
    most = Counter(items).most_common(1)[0][1]
    return round(most / len(items), 4)


def self_consistency(values: Sequence[Any]) -> float:
    """Fraction of a repeated discrete signal equal to the modal value."""
    return route_stability(values)


# ---------------------------------------------------------------------------
# Agreement — judge decision vs a hand-labelled golden set.
# ---------------------------------------------------------------------------
def agreement(predicted: Sequence[bool], expected: Sequence[bool]) -> float:
    """Fraction of predicted == expected (zipped to shorter). Empty -> 1.0."""
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
