"""Tests for Sleuth's LLM-as-judge answer evaluation (app/chatbot/evals.py).

The judge logic is pure (the model call is injected), so the unit tests run with
no network. A final DB-backed test pins the behaviour through the real cloud
path: a low-confidence verdict only appends a caveat, never rewrites the answer.
"""
from __future__ import annotations


# --- Verdict ----------------------------------------------------------------

def test_verdict_ok_property():
    from app.chatbot.evals import Verdict
    assert Verdict(grounded=True, faithful=True).ok is True
    assert Verdict(grounded=False, faithful=True).ok is False
    assert Verdict(grounded=True, faithful=False).ok is False


# --- build_judge_prompt -----------------------------------------------------

def test_build_judge_prompt_includes_sections_and_handles_empty_context():
    from app.chatbot.evals import build_judge_prompt
    p = build_judge_prompt("how many?", "#1 Login", "There is one.")
    assert "QUESTION:\nhow many?" in p
    assert "CONTEXT:\n#1 Login" in p
    assert "ANSWER:\nThere is one." in p
    empty = build_judge_prompt("hi", "   ", "hello")
    assert "(no records were retrieved)" in empty


# --- parse_verdict ----------------------------------------------------------

def test_parse_verdict_none_and_empty_return_none():
    from app.chatbot.evals import parse_verdict
    assert parse_verdict(None) is None
    assert parse_verdict({}) is None


def test_parse_verdict_coerces_and_clamps_score():
    from app.chatbot.evals import parse_verdict
    v = parse_verdict({"grounded": False, "faithful": True, "score": 0.3,
                       "issues": "cites #9 not in context"})
    assert v.grounded is False and v.faithful is True and v.score == 0.3
    assert v.issues == "cites #9 not in context"
    # Bad score type -> 0.0; out-of-range scores clamp into [0, 1].
    assert parse_verdict({"score": "not-a-number"}).score == 0.0
    assert parse_verdict({"score": 5}).score == 1.0
    assert parse_verdict({"score": -2}).score == 0.0
    # Missing fields default to "fine" so a good answer is never falsely flagged.
    d = parse_verdict({"score": 0.9})
    assert d.grounded is True and d.faithful is True and d.issues == ""


# --- judge ------------------------------------------------------------------

def test_judge_uses_injected_model():
    from app.chatbot.evals import judge
    seen = {}

    def call(prompt):
        seen["prompt"] = prompt
        return {"grounded": True, "faithful": True, "score": 0.8}

    v = judge("q", "ctx", "ans", call_model=call)
    assert v is not None and v.score == 0.8
    assert "QUESTION:\nq" in seen["prompt"]
    # A model failure (None) yields no verdict.
    assert judge("q", "ctx", "ans", call_model=lambda p: None) is None


# --- apply_verdict ----------------------------------------------------------

def test_apply_verdict_leaves_sound_answers_untouched():
    from app.chatbot.evals import apply_verdict, Verdict
    answer = "All good, see #1."
    assert apply_verdict(answer, None) == answer
    assert apply_verdict(answer, Verdict(score=0.9), min_score=0.5) == answer


def test_apply_verdict_appends_caveat_when_weak():
    from app.chatbot.evals import apply_verdict, Verdict
    # Low score triggers the caveat.
    low = apply_verdict("Maybe.", Verdict(score=0.2), min_score=0.5)
    assert "double-check" in low and low.startswith("Maybe.")
    # Ungrounded triggers it too, and the issue phrase is appended.
    ung = apply_verdict("See #9.", Verdict(grounded=False, score=0.9,
                                           issues="#9 not in context"))
    assert "#9 not in context" in ung


# --- DB-backed end-to-end through the real cloud path -----------------------

def _project(c, name="Proj"):
    r = c.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_cloud_answer_gets_caveat_from_judge(admin_client, monkeypatch):
    from app.database import SessionLocal
    from app.config import get_settings
    from app import models
    from app.chatbot import cloud_llm
    _project(admin_client)
    s = get_settings()
    monkeypatch.setattr(s, "SLEUTH_EVAL_ENABLED", True)
    monkeypatch.setattr(cloud_llm, "is_available", lambda: True)

    # The same provider serves the answer and the judge; branch on the system
    # prompt (the judge's begins "You are a strict evaluator").
    def fake(system, user):
        if "evaluator" in system:
            return '{"grounded": false, "faithful": true, "score": 0.1, "issues": "unsupported"}'
        return '{"mode":"answer","text":"I think there are dozens of open bugs."}'

    monkeypatch.setattr(cloud_llm, "_call_gemini", fake)
    db = SessionLocal()
    try:
        actor = db.query(models.User).first()
        resp = cloud_llm.try_understand("roughly how many bugs are open?", db, actor)
        assert resp is not None and resp.intent == "cloud_answer"
        text = resp.blocks[0].payload["text"]
        assert "dozens of open bugs" in text   # original answer preserved
        assert "double-check" in text          # judge caveat appended, not a rewrite
        assert "unsupported" in text           # the issue phrase surfaced
    finally:
        db.close()
