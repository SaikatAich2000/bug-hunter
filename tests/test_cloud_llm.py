"""Tests for app/chatbot/cloud_llm.py.

Covers the httpx provider bodies (and model-name validation), the circuit
breaker, the grounding/judge/answer helpers, the read-only agent loop closures,
and the recent-history fetch, all with fakes so nothing touches the network or
needs a live model.
"""
from __future__ import annotations

import sys
import types

import pytest

from app.chatbot import cloud_llm


def _settings(**over):
    base = {
        "SLEUTH_CLOUD_ENABLED": True,
        "GEMINI_API_KEY": "gk",
        "OPENROUTER_API_KEY": "ok",
        "GEMINI_MODEL": "gemini-2.5-flash",
        "OPENROUTER_MODEL": "vendor/model:free",
        "SLEUTH_CLOUD_MAX_TOKENS": 256,
        "SLEUTH_CLOUD_TIMEOUT_S": 5,
        "SLEUTH_RETRIEVAL_ENABLED": True,
        "SLEUTH_VERIFY_ANSWERS": False,
        "SLEUTH_EVAL_ENABLED": False,
        "SLEUTH_EVAL_MIN_SCORE": 0.5,
        "SLEUTH_AGENT_ENABLED": False,
        "SLEUTH_AGENT_MAX_STEPS": 3,
    }
    base.update(over)
    return types.SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _reset_cooldown(monkeypatch):
    # Rebind to the current generation (the conftest `client` fixture purges
    # app.* from sys.modules) so monkeypatches land on the module cloud_llm's
    # internal imports actually resolve to.
    import importlib
    global cloud_llm
    cloud_llm = importlib.import_module("app.chatbot.cloud_llm")
    monkeypatch.setattr(cloud_llm, "_cooldown_until", 0.0)
    yield


def _install_httpx(monkeypatch, *, payload=None, raise_status=False, post_raises=False):
    hx = types.ModuleType("httpx")
    captured = {}

    class _R:
        def raise_for_status(self):
            if raise_status:
                raise RuntimeError("HTTP 500")

        def json(self):
            return payload

    def post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json)
        if post_raises:
            raise RuntimeError("net down")
        return _R()

    hx.post = post
    hx._captured = captured
    monkeypatch.setitem(sys.modules, "httpx", hx)
    return hx


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------
def test_is_available_disabled(monkeypatch):
    monkeypatch.setattr(cloud_llm, "get_settings", lambda: _settings(SLEUTH_CLOUD_ENABLED=False))
    assert cloud_llm.is_available() is False


def test_is_available_no_key(monkeypatch):
    monkeypatch.setattr(cloud_llm, "get_settings",
                        lambda: _settings(GEMINI_API_KEY="", OPENROUTER_API_KEY=""))
    assert cloud_llm.is_available() is False


def test_is_available_true(monkeypatch):
    monkeypatch.setattr(cloud_llm, "get_settings", lambda: _settings())
    _install_httpx(monkeypatch)
    assert cloud_llm.is_available() is True


# ---------------------------------------------------------------------------
# _call_gemini
# ---------------------------------------------------------------------------
def test_call_gemini_no_key(monkeypatch):
    monkeypatch.setattr(cloud_llm, "get_settings", lambda: _settings(GEMINI_API_KEY=""))
    assert cloud_llm._call_gemini("s", "u") is None


def test_call_gemini_bad_model_rejected(monkeypatch):
    monkeypatch.setattr(cloud_llm, "get_settings",
                        lambda: _settings(GEMINI_MODEL="evil/../../path"))
    assert cloud_llm._call_gemini("s", "u") is None


def test_call_gemini_success(monkeypatch):
    monkeypatch.setattr(cloud_llm, "get_settings", lambda: _settings())
    payload = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}
    hx = _install_httpx(monkeypatch, payload=payload)
    assert cloud_llm._call_gemini("sys", "usr") == "hello"
    assert hx._captured["headers"]["x-goog-api-key"] == "gk"
    assert "key=" not in hx._captured["url"]


def test_call_gemini_http_error(monkeypatch):
    monkeypatch.setattr(cloud_llm, "get_settings", lambda: _settings())
    _install_httpx(monkeypatch, raise_status=True)
    assert cloud_llm._call_gemini("s", "u") is None


# ---------------------------------------------------------------------------
# _call_openrouter
# ---------------------------------------------------------------------------
def test_call_openrouter_no_key(monkeypatch):
    monkeypatch.setattr(cloud_llm, "get_settings", lambda: _settings(OPENROUTER_API_KEY=""))
    assert cloud_llm._call_openrouter("s", "u") is None


def test_call_openrouter_bad_model_rejected(monkeypatch):
    monkeypatch.setattr(cloud_llm, "get_settings",
                        lambda: _settings(OPENROUTER_MODEL="bad model with spaces"))
    assert cloud_llm._call_openrouter("s", "u") is None


def test_call_openrouter_success(monkeypatch):
    monkeypatch.setattr(cloud_llm, "get_settings", lambda: _settings())
    payload = {"choices": [{"message": {"content": "answer"}}]}
    hx = _install_httpx(monkeypatch, payload=payload)
    assert cloud_llm._call_openrouter("sys", "usr") == "answer"
    assert hx._captured["headers"]["Authorization"] == "Bearer ok"


def test_call_openrouter_post_raises(monkeypatch):
    monkeypatch.setattr(cloud_llm, "get_settings", lambda: _settings())
    _install_httpx(monkeypatch, post_raises=True)
    assert cloud_llm._call_openrouter("s", "u") is None


# ---------------------------------------------------------------------------
# Cooldown + _complete
# ---------------------------------------------------------------------------
def test_cooldown_trip_and_in(monkeypatch):
    assert cloud_llm._in_cooldown() is False
    cloud_llm._trip_cooldown()
    assert cloud_llm._in_cooldown() is True


def test_complete_short_circuits_in_cooldown(monkeypatch):
    cloud_llm._trip_cooldown()
    called = {"n": 0}
    monkeypatch.setattr(cloud_llm, "_call_gemini", lambda s, u: called.__setitem__("n", 1))
    assert cloud_llm._complete("s", "u") is None
    assert called["n"] == 0  # never reached the provider


def test_complete_success(monkeypatch):
    monkeypatch.setattr(cloud_llm, "_call_gemini", lambda s, u: '{"mode":"answer","text":"hi"}')
    assert cloud_llm._complete("s", "u") == {"mode": "answer", "text": "hi"}


def test_complete_both_fail_trips_cooldown(monkeypatch):
    monkeypatch.setattr(cloud_llm, "_call_gemini", lambda s, u: None)
    monkeypatch.setattr(cloud_llm, "_call_openrouter", lambda s, u: None)
    assert cloud_llm._complete("s", "u") is None
    assert cloud_llm._in_cooldown() is True


def test_complete_unparseable_trips_cooldown(monkeypatch):
    monkeypatch.setattr(cloud_llm, "_call_gemini", lambda s, u: "not json at all")
    assert cloud_llm._complete("s", "u") is None
    assert cloud_llm._in_cooldown() is True


def test_complete_subcall_does_not_trip(monkeypatch):
    monkeypatch.setattr(cloud_llm, "_call_gemini", lambda s, u: None)
    monkeypatch.setattr(cloud_llm, "_call_openrouter", lambda s, u: None)
    assert cloud_llm._complete("s", "u", trip_cooldown=False) is None
    assert cloud_llm._in_cooldown() is False


def test_complete_json_is_subcall(monkeypatch):
    seen = {}
    monkeypatch.setattr(cloud_llm, "_complete",
                        lambda s, u, trip_cooldown=True: seen.update(trip=trip_cooldown) or {"ok": 1})
    assert cloud_llm.complete_json("s", "u") == {"ok": 1}
    assert seen["trip"] is False


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------
def test_extract_json_wrapping_fence():
    assert cloud_llm._extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_plain():
    assert cloud_llm._extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_non_dict_then_dict():
    assert cloud_llm._extract_json('[1] {"a": 2}') == {"a": 2}


def test_extract_json_none():
    assert cloud_llm._extract_json("no json here") is None


def test_strip_wrapping_fence_variants():
    f = cloud_llm._strip_wrapping_fence
    # ```json\n...\n``` and bare ```\n...\n``` both unwrap.
    assert f('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert f('```\n{"a": 1}\n```') == '{"a": 1}'
    # Single-line fence (no newline) unwraps too.
    assert f('```{"a": 1}```') == '{"a": 1}'
    # Not fence-wrapped → returned (stripped) unchanged.
    assert f('  {"a": 1}  ') == '{"a": 1}'
    # A fence inside a JSON string value (payload not wholly fenced) survives.
    assert f('{"text": "use ```code```"}') == '{"text": "use ```code```"}'


def test_extract_json_bare_fence():
    assert cloud_llm._extract_json('```\n{"a": 1}\n```') == {"a": 1}


# ---------------------------------------------------------------------------
# _grounding
# ---------------------------------------------------------------------------
def test_grounding_retrieval_and_rag(monkeypatch):
    from app.chatbot import retrieval, rag
    hit = types.SimpleNamespace(id=7)
    monkeypatch.setattr(retrieval, "retrieve_bugs", lambda db, msg: [hit])
    monkeypatch.setattr(retrieval, "format_context", lambda hits: "KEYWORD CTX")
    monkeypatch.setattr(rag, "retrieve_text", lambda msg: "RAG CTX")
    text, ids = cloud_llm._grounding("q", None, _settings())
    assert "KEYWORD CTX" in text and "RAG CTX" in text
    assert ids == {7}


def test_grounding_disabled_and_no_rag(monkeypatch):
    from app.chatbot import rag
    monkeypatch.setattr(rag, "retrieve_text", lambda msg: "")
    text, ids = cloud_llm._grounding("q", None, _settings(SLEUTH_RETRIEVAL_ENABLED=False))
    assert text == "" and ids == set()


def test_grounding_retrieval_raises(monkeypatch):
    from app.chatbot import retrieval, rag

    def _boom(db, msg):
        raise RuntimeError("retrieval down")

    monkeypatch.setattr(retrieval, "retrieve_bugs", _boom)
    monkeypatch.setattr(rag, "retrieve_text", lambda msg: "")
    text, ids = cloud_llm._grounding("q", None, _settings())
    assert text == "" and ids == set()


def test_grounding_rag_raises(monkeypatch):
    from app.chatbot import retrieval, rag
    monkeypatch.setattr(retrieval, "retrieve_bugs", lambda db, msg: [])
    monkeypatch.setattr(retrieval, "format_context", lambda hits: "")

    def _boom(msg):
        raise RuntimeError("rag down")

    monkeypatch.setattr(rag, "retrieve_text", _boom)
    text, _ids = cloud_llm._grounding("q", None, _settings())
    assert text == ""


# ---------------------------------------------------------------------------
# _judge_text
# ---------------------------------------------------------------------------
def test_judge_text_applies_verdict(monkeypatch):
    from app.chatbot import evals
    monkeypatch.setattr(evals, "judge", lambda m, c, t, call_model: {"score": 0.9})
    monkeypatch.setattr(evals, "apply_verdict", lambda t, v, min_score: t + " [judged]")
    assert cloud_llm._judge_text("m", "c", "answer", _settings()) == "answer [judged]"


def test_judge_text_fails_open(monkeypatch):
    from app.chatbot import evals

    def _boom(*a, **k):
        raise RuntimeError("judge down")

    monkeypatch.setattr(evals, "judge", _boom)
    assert cloud_llm._judge_text("m", "c", "answer", _settings()) == "answer"


# ---------------------------------------------------------------------------
# _answer_response
# ---------------------------------------------------------------------------
def test_answer_response_empty_returns_none():
    assert cloud_llm._answer_response("   ", _settings(), set()) is None


def test_answer_response_plain():
    resp = cloud_llm._answer_response("Just a reply", _settings(), set())
    assert resp.intent == "cloud_answer"
    assert resp.blocks[0].payload["text"] == "Just a reply"


def test_answer_response_verifies_and_judges(monkeypatch):
    from app.chatbot import verify
    monkeypatch.setattr(verify, "annotate", lambda text, ids: text + " [verified]")
    monkeypatch.setattr(cloud_llm, "_judge_text", lambda m, c, t, s: t + " [judged]")
    resp = cloud_llm._answer_response(
        "Bug #9 detail", _settings(SLEUTH_VERIFY_ANSWERS=True, SLEUTH_EVAL_ENABLED=True),
        {9}, message="m", context="c",
    )
    assert resp.blocks[0].payload["text"] == "Bug #9 detail [verified] [judged]"


# ---------------------------------------------------------------------------
# _run_agent (drive the bound closures via a fake agent)
# ---------------------------------------------------------------------------
def _install_fake_agent(monkeypatch, result, *, capture=None):
    agent = sys.modules.get("app.chatbot.agent")
    if agent is None:  # import lazily like the module does
        from app.chatbot import agent  # noqa: F811
    monkeypatch.setattr(agent, "AGENT_SYSTEM", "AGENT_SYS", raising=False)
    monkeypatch.setattr(agent, "summarize_response", lambda r: "SUMMARY", raising=False)

    def fake_run_agent(message, *, call_model, run_query, run_retrieve,
                       max_steps, history, context):
        if capture is not None:
            capture["model"] = call_model("step prompt")
            capture["query"] = run_query("open bugs")
            capture["retrieve"] = run_retrieve("login")
        return result

    monkeypatch.setattr(agent, "run_agent", fake_run_agent, raising=False)
    return agent


def test_run_agent_data(monkeypatch):
    cap = {}
    _install_fake_agent(
        monkeypatch,
        types.SimpleNamespace(kind="data", canonical_query="open bugs", text="", grounded_ids=set()),
        capture=cap,
    )
    monkeypatch.setattr(cloud_llm, "_complete", lambda s, p, trip_cooldown=True: {"x": 1})
    sentinel = object()
    monkeypatch.setattr(cloud_llm, "_route_data_query", lambda c, db, actor, now: sentinel)
    out = cloud_llm._run_agent("msg", None, None, _now(), "", "ctx", set(), _settings())
    assert out is sentinel
    assert cap["model"] == {"x": 1}
    assert cap["query"] == "SUMMARY"
    assert cap["retrieve"][0] == "" or isinstance(cap["retrieve"], tuple)


def test_run_agent_text(monkeypatch):
    _install_fake_agent(
        monkeypatch,
        types.SimpleNamespace(kind="text", canonical_query="", text="An answer", grounded_ids={3}),
    )
    monkeypatch.setattr(cloud_llm, "_complete", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(cloud_llm, "_answer_response",
                        lambda text, s, ids, message="", context="": types.SimpleNamespace(text=text, ids=ids))
    out = cloud_llm._run_agent("msg", None, None, _now(), "", "ctx", {1}, _settings())
    assert out.text == "An answer"
    assert out.ids == {1, 3}


def test_run_agent_none(monkeypatch):
    _install_fake_agent(
        monkeypatch,
        types.SimpleNamespace(kind="other", canonical_query="", text="", grounded_ids=set()),
    )
    assert cloud_llm._run_agent("msg", None, None, _now(), "", "", set(), _settings()) is None


def test_run_agent_call_model_deadline(monkeypatch):
    cap = {}
    _install_fake_agent(
        monkeypatch,
        types.SimpleNamespace(kind="none", canonical_query="", text="", grounded_ids=set()),
        capture=cap,
    )
    # timeout=0 → deadline == entry time, so call_model's monotonic check trips.
    cloud_llm._run_agent("msg", None, None, _now(), "", "", set(),
                         _settings(SLEUTH_CLOUD_TIMEOUT_S=0))
    assert cap["model"] is None


def test_run_agent_query_tool_error(monkeypatch):
    cap = {}
    _install_fake_agent(
        monkeypatch,
        types.SimpleNamespace(kind="none", canonical_query="", text="", grounded_ids=set()),
        capture=cap,
    )
    monkeypatch.setattr(cloud_llm, "_complete", lambda *a, **k: None)

    def _boom(c, db, actor, now):
        raise RuntimeError("query failed")

    monkeypatch.setattr(cloud_llm, "_route_data_query", _boom)
    cloud_llm._run_agent("msg", None, None, _now(), "", "", set(), _settings())
    assert cap["query"] == "The query could not be run."


# ---------------------------------------------------------------------------
# _recent_history
# ---------------------------------------------------------------------------
class _Q:
    def __init__(self, first=None, all_=None):
        self._first = first
        self._all = all_

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._all


class _DB:
    def __init__(self, queries):
        self._queries = list(queries)

    def query(self, _model):
        return self._queries.pop(0)


def _now():
    from datetime import datetime, timezone
    return datetime(2026, 6, 21, tzinfo=timezone.utc)


def test_recent_history_no_conversation():
    actor = types.SimpleNamespace(id=1)
    assert cloud_llm._recent_history(_DB([_Q(first=None)]), actor) == ""


def test_recent_history_returns_lines():
    actor = types.SimpleNamespace(id=1)
    conv = types.SimpleNamespace(id=10)
    msgs = [
        types.SimpleNamespace(role="assistant", content="second"),
        types.SimpleNamespace(role="user", content="first"),
    ]  # query returns desc; module reverses to oldest-first
    db = _DB([_Q(first=conv), _Q(all_=msgs)])
    out = cloud_llm._recent_history(db, actor)
    assert out == "user: first\nassistant: second"


def test_recent_history_swallows_errors():
    class _Boom:
        def query(self, _m):
            raise RuntimeError("db down")

    assert cloud_llm._recent_history(_Boom(), types.SimpleNamespace(id=1)) == ""


# ---------------------------------------------------------------------------
# is_available — httpx import failure (66-67)
# ---------------------------------------------------------------------------
def test_is_available_httpx_missing(monkeypatch):
    monkeypatch.setattr(cloud_llm, "get_settings", lambda: _settings())
    monkeypatch.setitem(sys.modules, "httpx", None)  # `import httpx` → ImportError
    assert cloud_llm.is_available() is False


# ---------------------------------------------------------------------------
# _complete subcall unparseable (262->266: skip trip when trip_cooldown=False)
# ---------------------------------------------------------------------------
def test_complete_subcall_unparseable_no_trip(monkeypatch):
    monkeypatch.setattr(cloud_llm, "_call_gemini", lambda s, u: "totally not json")
    assert cloud_llm._complete("s", "u", trip_cooldown=False) is None
    assert cloud_llm._in_cooldown() is False


# ---------------------------------------------------------------------------
# try_understand branches
# ---------------------------------------------------------------------------
def test_try_understand_unavailable(monkeypatch):
    monkeypatch.setattr(cloud_llm, "is_available", lambda: False)
    assert cloud_llm.try_understand("q", None, None) is None


def test_try_understand_cooldown_short_circuit(monkeypatch):
    monkeypatch.setattr(cloud_llm, "is_available", lambda: True)
    monkeypatch.setattr(cloud_llm, "_in_cooldown", lambda: True)
    assert cloud_llm.try_understand("q", None, None) is None  # returns before touching db


def test_try_understand_agent_path(monkeypatch):
    monkeypatch.setattr(cloud_llm, "is_available", lambda: True)
    monkeypatch.setattr(cloud_llm, "_in_cooldown", lambda: False)
    monkeypatch.setattr(cloud_llm, "_recent_history", lambda db, actor: "prior")
    monkeypatch.setattr(cloud_llm, "_grounding", lambda m, db, s: ("", set()))
    monkeypatch.setattr(cloud_llm, "get_settings", lambda: _settings(SLEUTH_AGENT_ENABLED=True))
    sentinel = object()
    monkeypatch.setattr(cloud_llm, "_run_agent", lambda *a, **k: sentinel)
    assert cloud_llm.try_understand("q", None, None) is sentinel


def test_try_understand_agent_none_falls_through(monkeypatch):
    monkeypatch.setattr(cloud_llm, "is_available", lambda: True)
    monkeypatch.setattr(cloud_llm, "_in_cooldown", lambda: False)
    monkeypatch.setattr(cloud_llm, "_grounding", lambda m, db, s: ("", set()))
    monkeypatch.setattr(cloud_llm, "get_settings", lambda: _settings(SLEUTH_AGENT_ENABLED=True))
    monkeypatch.setattr(cloud_llm, "_run_agent", lambda *a, **k: None)
    sentinel = object()
    monkeypatch.setattr(cloud_llm, "_single_shot", lambda *a, **k: sentinel)
    # history supplied → _recent_history is skipped (412->416 branch)
    assert cloud_llm.try_understand("q", None, None, history="prior turn") is sentinel


def test_try_understand_single_shot_path(monkeypatch):
    monkeypatch.setattr(cloud_llm, "is_available", lambda: True)
    monkeypatch.setattr(cloud_llm, "_in_cooldown", lambda: False)
    monkeypatch.setattr(cloud_llm, "_recent_history", lambda db, actor: "")
    monkeypatch.setattr(cloud_llm, "_grounding", lambda m, db, s: ("", set()))
    monkeypatch.setattr(cloud_llm, "get_settings", lambda: _settings(SLEUTH_AGENT_ENABLED=False))
    sentinel = object()
    monkeypatch.setattr(cloud_llm, "_single_shot", lambda *a, **k: sentinel)
    assert cloud_llm.try_understand("q", None, None) is sentinel


# ---------------------------------------------------------------------------
# _single_shot branches
# ---------------------------------------------------------------------------
def test_single_shot_parsed_none(monkeypatch):
    monkeypatch.setattr(cloud_llm, "_complete", lambda s, p: None)
    assert cloud_llm._single_shot("m", None, None, _now(), "", "", set(), _settings()) is None


def test_single_shot_data_mode(monkeypatch):
    monkeypatch.setattr(cloud_llm, "_complete",
                        lambda s, p: {"mode": "data", "canonical_query": "open bugs"})
    sentinel = object()
    monkeypatch.setattr(cloud_llm, "_route_data_query", lambda c, db, actor, now: sentinel)
    # history + context both present → both prompt-building branches run.
    out = cloud_llm._single_shot("m", None, None, _now(), "hist", "CTX", set(), _settings())
    assert out is sentinel


def test_single_shot_answer_mode(monkeypatch):
    monkeypatch.setattr(cloud_llm, "_complete", lambda s, p: {"mode": "answer", "text": "hi"})
    sentinel = object()
    monkeypatch.setattr(cloud_llm, "_answer_response", lambda *a, **k: sentinel)
    assert cloud_llm._single_shot("m", None, None, _now(), "", "", set(), _settings()) is sentinel


def test_single_shot_unknown_mode(monkeypatch):
    monkeypatch.setattr(cloud_llm, "_complete", lambda s, p: {"mode": "weird"})
    assert cloud_llm._single_shot("m", None, None, _now(), "", "", set(), _settings()) is None


# ---------------------------------------------------------------------------
# _route_data_query branches
# ---------------------------------------------------------------------------
def _patch_route(monkeypatch, *, intent, resp):
    import importlib
    ex = importlib.import_module("app.chatbot.executor")
    nlu = importlib.import_module("app.chatbot.nlu")
    monkeypatch.setattr(ex, "build_context", lambda db: "ctx")
    monkeypatch.setattr(nlu, "parse", lambda c, ctx, now=None: types.SimpleNamespace(intent=intent))
    monkeypatch.setattr(ex, "_dispatch_read_intent",
                        lambda intent_, db, pq, actor, ctx, read_only=False: resp)


def test_route_empty_canonical(monkeypatch):
    assert cloud_llm._route_data_query("   ", None, None, _now()) is None


def test_route_drops_action_intent(monkeypatch):
    _patch_route(monkeypatch, intent="action_close", resp=object())
    assert cloud_llm._route_data_query("close 5", None, None, _now()) is None


def test_route_dispatch_none(monkeypatch):
    _patch_route(monkeypatch, intent="list_bugs", resp=None)
    assert cloud_llm._route_data_query("open bugs", None, None, _now()) is None


def test_route_dispatch_tags_intent(monkeypatch):
    resp = types.SimpleNamespace(intent="list_bugs")
    _patch_route(monkeypatch, intent="list_bugs", resp=resp)
    out = cloud_llm._route_data_query("open bugs", None, None, _now())
    assert out.intent == "cloud_data:list_bugs"
