"""Offline eval harness tests (app/chatbot/eval_harness.py): llm_judge, trajectory,
outcome, confidence, reliability, pass@k — pure units plus stubbed-model wiring."""
from __future__ import annotations


# Test helpers
def _project(c, name="Proj"):
    r = c.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _bug(c, pid, title, description=""):
    r = c.post("/api/bugs", json={
        "project_id": pid, "title": title, "description": description,
        "priority": "Medium", "environment": "DEV",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _scripted(steps):
    """Return a call_model that yields scripted step dicts one at a time."""
    seq = list(steps)
    return lambda prompt: seq.pop(0)


# 1. Trajectory — score the read-only agent's tool-use path
def test_score_trajectory_shapes():
    from app.chatbot.eval_harness import score_trajectory
    # Well-formed, grounded, efficient run.
    good = score_trajectory(["retrieve", "query", "final"], max_steps=4, grounded_final=True)
    assert good.valid and good.efficient and good.grounded_final and good.ok
    assert good.steps == 3 and good.score == 1.0
    # Valid but ungrounded -> lower score, not ok.
    ungrounded = score_trajectory(["query", "final"], max_steps=4, grounded_final=False)
    assert ungrounded.valid and not ungrounded.grounded_final and not ungrounded.ok
    assert ungrounded.score == 0.7
    # Terminal in the middle is invalid; a tool step must precede the finish.
    assert not score_trajectory(["final", "query"], max_steps=4, grounded_final=True).valid
    # No terminal at all -> invalid (the loop gave up).
    assert not score_trajectory(["query", "query"], max_steps=4, grounded_final=False).valid
    # Unknown action -> invalid. Empty -> invalid, score 0.
    assert not score_trajectory(["frobnicate", "final"], max_steps=4, grounded_final=True).valid
    empty = score_trajectory([], max_steps=4, grounded_final=True)
    assert not empty.valid and empty.steps == 0 and empty.score == 0.0
    # Over-budget: valid but not efficient.
    over = score_trajectory(["query", "query", "query", "final"], max_steps=2, grounded_final=True)
    assert over.valid and not over.efficient and over.score == 0.8


def test_run_and_trace_terminal_paths():
    from app.chatbot import eval_harness as eh
    # retrieve -> query -> final: grounded text answer.
    steps = [
        {"action": "retrieve", "query": "login"},
        {"action": "query", "canonical_query": "open bugs"},
        {"action": "final", "text": "There is one open bug, #1."},
    ]
    result, actions = eh.run_and_trace(
        "why does login fail", call_model=_scripted(steps),
        run_query=lambda c: "1 row(s): #1 Login (New)",
        run_retrieve=lambda q: ("#1 Login", {1}), max_steps=4,
    )
    assert actions == ["retrieve", "query", "final"]
    assert result.kind == "text" and result.grounded_ids == {1}
    ts = eh.score_trajectory(actions, max_steps=4, grounded_final=bool(result.grounded_ids))
    assert ts.ok and ts.score == 1.0

    # answer_data: the loop returns a canonical query for a real table lookup.
    result, actions = eh.run_and_trace(
        "list open", call_model=_scripted([{"action": "answer_data", "canonical_query": "open bugs"}]),
        run_query=lambda c: "x", run_retrieve=lambda q: ("", set()), max_steps=4,
    )
    assert actions == ["answer_data"] and result.kind == "data"
    assert eh.score_trajectory(actions, max_steps=4, grounded_final=True).ok

    # Never terminates -> kind "none", no terminal action in the trace.
    result, actions = eh.run_and_trace(
        "loop", call_model=_scripted([{"action": "query", "canonical_query": "open bugs"}] * 5),
        run_query=lambda c: "x", run_retrieve=lambda q: ("", set()), max_steps=3,
    )
    assert actions == ["query", "query", "query"] and result.kind == "none"
    assert not eh.score_trajectory(actions, max_steps=3, grounded_final=False).valid


# 2. Outcome — did the final Response achieve the goal?
def test_check_outcome_pure():
    import types
    from app.chatbot.eval_harness import check_outcome
    resp = types.SimpleNamespace(
        intent="cloud_data:list_bugs",
        blocks=[types.SimpleNamespace(kind="text", payload={"text": "Found 2 bugs"}),
                types.SimpleNamespace(kind="table", payload={"rows": []})],
    )
    assert check_outcome(resp, {"intent_prefix": "cloud_data:", "has_table": True,
                                "text_contains": "found 2"}).ok
    bad = check_outcome(resp, {"intent": "greeting", "has_file": True,
                               "text_contains": "nope"})
    assert not bad.ok and len(bad.reasons) == 3
    # Wrong intent prefix and missing table block.
    miss = check_outcome(
        types.SimpleNamespace(intent="greeting", blocks=[]),
        {"intent_prefix": "cloud_data:", "has_table": True},
    )
    assert not miss.ok and len(miss.reasons) == 2
    assert not check_outcome(None, {}).ok


def test_outcome_over_executor_golden_tasks(admin_client):
    """Golden corpus through the real executor: each Response matches its goal shape."""
    from app.database import SessionLocal
    from app import models
    from app.chatbot import executor
    from app.chatbot.eval_harness import check_outcome
    pid = _project(admin_client)
    _bug(admin_client, pid, "Login crash", "boom")
    golden = [
        ("list all bugs", {"intent": "list_bugs", "has_table": True}),
        ("how many bugs are open?", {"intent": "count_bugs"}),
        ("summary", {"intent": "stats"}),
        ("list projects", {"intent": "list_projects", "has_table": True}),
    ]
    db = SessionLocal()
    try:
        actor = db.query(models.User).first()
        for message, spec in golden:
            resp = executor.execute(message, db, actor)
            res = check_outcome(resp, spec)
            assert res.ok, (message, res.reasons)
    finally:
        db.close()


# 3 & 6. LLM-judge, agreement and confidence (calibration)
def test_agreement_and_brier_pure():
    from app.chatbot.eval_harness import agreement, brier_score
    assert agreement([], []) == 1.0
    assert agreement([True, False, True], [True, False, False]) == round(2 / 3, 4)
    assert brier_score([]) == 0.0
    assert brier_score([(1.0, True), (0.0, False)]) == 0.0      # perfect
    assert brier_score([(1.0, False), (0.0, True)]) == 1.0      # worst
    assert brier_score([(0.9, True), (0.2, False)]) == 0.025    # well-calibrated


def test_llm_judge_agreement_and_confidence_over_golden_set():
    """Stubbed LLM-judge over a labelled golden set: decisions match labels, confidence calibrated."""
    from app.chatbot import evals
    from app.chatbot import eval_harness as eh
    golden = [
        ("how many open?", "#1 Login (New)", "There is 1 open bug, #1.",
         True, {"grounded": True, "faithful": True, "score": 0.9}),
        ("how many open?", "#1 Login (New)", "There are 5 open bugs; see #7.",
         False, {"grounded": False, "faithful": False, "score": 0.2,
                 "issues": "#7 not in context"}),
    ]
    predicted, expected, conf_pairs = [], [], []
    for q, ctx, ans, exp_ok, verdict in golden:
        v = evals.judge(q, ctx, ans, call_model=lambda p, _v=verdict: _v)
        decision_ok = v.ok and v.score >= 0.5
        predicted.append(decision_ok)
        expected.append(exp_ok)
        conf_pairs.append((v.score, exp_ok))
    assert eh.agreement(predicted, expected) == 1.0
    assert eh.brier_score(conf_pairs) <= 0.1
    # Re-judging the same case should be stable.
    q, ctx, ans, _ok, verdict = golden[0]
    repeats = [evals.judge(q, ctx, ans, call_model=lambda p: verdict).score for _ in range(3)]
    assert eh.self_consistency(repeats) == 1.0


# 4. Reliability — stability of answers/routes and live counters
def test_reliability_metrics_pure():
    from app.chatbot.eval_harness import answer_variance, route_stability, self_consistency
    assert answer_variance([]) == 0.0
    assert answer_variance(["same", "Same", "  same  "]) == round(1 / 3, 4)  # all equal
    assert answer_variance(["a", "b", "c"]) == 1.0                           # all distinct
    assert route_stability([]) == 1.0
    assert route_stability(["data", "data", "data"]) == 1.0
    assert route_stability(["data", "data", "answer"]) == round(2 / 3, 4)
    assert self_consistency(["x", "x", "y"]) == round(2 / 3, 4)


def test_reliability_route_determinism_on_deterministic_layer(admin_client):
    """Deterministic parse+dispatch returns the same intent every run (LLM layer excluded)."""
    from app.database import SessionLocal
    from app import models
    from app.chatbot import executor
    from app.chatbot.eval_harness import self_consistency
    pid = _project(admin_client)
    _bug(admin_client, pid, "Login crash", "boom")
    db = SessionLocal()
    try:
        actor = db.query(models.User).first()
        for msg in ["list all bugs", "how many open bugs?", "summary",
                    "list projects", "help", "hi there"]:
            intents = [executor.execute(msg, db, actor).intent for _ in range(3)]
            assert self_consistency(intents) == 1.0, (msg, intents)
    finally:
        db.close()


def test_reliability_live_counters(admin_client, monkeypatch):
    """Live counters record which provider served the turn and which route was chosen."""
    from app.config import get_settings
    from app.database import SessionLocal
    from app import models
    from app.chatbot import cloud_llm, executor
    pid = _project(admin_client)
    _bug(admin_client, pid, "Login crash", "boom")
    s = get_settings()
    monkeypatch.setattr(s, "SLEUTH_CLOUD_ENABLED", True)
    monkeypatch.setattr(s, "GROQ_API_KEY", "k")
    monkeypatch.setattr(cloud_llm, "_cooldown_until", 0.0, raising=False)
    monkeypatch.setattr(cloud_llm, "is_available", lambda: True)
    cloud_llm._reset_metrics_for_test()
    monkeypatch.setattr(
        cloud_llm, "_call_groq",
        lambda system, user, **kw: '{"mode":"data","canonical_query":"list all bugs"}',
    )
    db = SessionLocal()
    try:
        actor = db.query(models.User).first()
        executor.execute("what is still outstanding?", db, actor)
    finally:
        db.close()
    snap = cloud_llm.metrics_snapshot()
    assert snap.get("provider:groq", 0) >= 1
    assert snap.get("route:data", 0) >= 1


def test_cooldown_no_ratchet_and_trip_counter(monkeypatch):
    from app.chatbot import cloud_llm
    monkeypatch.setattr(cloud_llm, "_cooldown_until", 0.0, raising=False)
    cloud_llm._reset_metrics_for_test()
    cloud_llm._trip_cooldown()
    first = cloud_llm._cooldown_until
    # A second trip while already in cooldown must not extend the window or recount.
    cloud_llm._trip_cooldown()
    assert cloud_llm._cooldown_until == first
    assert cloud_llm.metrics_snapshot().get("cooldown_trips") == 1


# 5. pass@k — fraction of tasks where at least one of K attempts passed
def test_pass_at_k_pure():
    from app.chatbot.eval_harness import pass_at_k, aggregate_pass_at_k
    assert pass_at_k([False, True, False]) == 1.0
    assert pass_at_k([False, False]) == 0.0
    assert aggregate_pass_at_k([]) == 0.0
    # task0 passes on the second attempt, task1 never passes -> 0.5 mean.
    assert aggregate_pass_at_k([[False, True], [False, False]]) == 0.5


def test_pass_at_k_over_outcome_checks(admin_client):
    """A deterministic data task passes every attempt, so pass@k and aggregate are 1.0."""
    from app.database import SessionLocal
    from app import models
    from app.chatbot import executor
    from app.chatbot.eval_harness import check_outcome, pass_at_k, aggregate_pass_at_k
    pid = _project(admin_client)
    _bug(admin_client, pid, "Login crash", "boom")
    spec = {"intent": "list_bugs", "has_table": True}
    K = 3
    db = SessionLocal()
    try:
        actor = db.query(models.User).first()
        samples = [check_outcome(executor.execute("list all bugs", db, actor), spec).ok
                   for _ in range(K)]
        assert pass_at_k(samples) == 1.0
        assert aggregate_pass_at_k([samples]) == 1.0
    finally:
        db.close()
