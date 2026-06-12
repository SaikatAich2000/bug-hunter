"""Tests for Sleuth's cloud LLM layer (Gemini / OpenRouter fallback).

These never hit the network — the provider calls are monkeypatched. They
pin the safety contract:

  - OFF by default (no key / flag => layer invisible).
  - Data questions are answered by the SAME deterministic SQL path as the
    rule engine — the model only picks the filter, so numbers are real and
    identical to a direct rule query ("never guess").
  - The cloud layer can NEVER initiate a write, even if the model emits a
    close/delete/assign canonical query.
  - Gemini failure falls back to OpenRouter.
  - Everything sent outbound is redacted first.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["SESSION_SECRET"] = "test-cloud"

import pytest  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.chatbot import executor, cloud_llm  # noqa: E402
from app.chatbot.memory import store as memstore  # noqa: E402
from app.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _rebind_app_modules():
    """Keep this file's module references on the live generation.

    The conftest `client` fixture purges every `app.*` entry from sys.modules
    (it does `del sys.modules[mod]` to force an engine re-import). Any test
    running after one of those would otherwise hold a STALE `executor` /
    `cloud_llm` / `engine`, while executor's internal `import cloud_llm`
    resolves to a fresh generation — so a mock placed on our reference would
    be invisible inside execute(), and the seeded DB (our engine) would differ
    from the one executor queries. Re-importing all of them together each test
    guarantees one consistent generation. (Production never purges modules;
    this is purely test hygiene.)
    """
    import importlib
    g = globals()
    db_mod = importlib.import_module("app.database")
    g["Base"], g["engine"], g["SessionLocal"] = (
        db_mod.Base, db_mod.engine, db_mod.SessionLocal,
    )
    g["models"] = importlib.import_module("app.models")
    g["executor"] = importlib.import_module("app.chatbot.executor")
    g["cloud_llm"] = importlib.import_module("app.chatbot.cloud_llm")
    g["memstore"] = importlib.import_module("app.chatbot.memory").store
    g["hash_password"] = importlib.import_module("app.auth").hash_password
    g["get_settings"] = importlib.import_module("app.config").get_settings
    yield


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    memstore._clear_all_for_test()
    s = SessionLocal()
    try:
        admin = models.User(name="Admin", email="admin@example.com", role="admin",
                            password_hash=hash_password("AdminPass123!"), is_active=True)
        john = models.User(name="John", email="john@example.com", role="manager",
                           password_hash=hash_password("JohnPass123!"), is_active=True)
        s.add_all([admin, john]); s.flush()
        proj = models.Project(name="Mobile", description="")
        s.add(proj); s.flush()
        # 2 open + 1 closed work item.
        s.add_all([
            models.Bug(project_id=proj.id, title="Crash on login", status="New",
                       priority="High", environment="PROD", reporter_id=john.id),
            models.Bug(project_id=proj.id, title="Slow dashboard", status="In Progress",
                       priority="Medium", environment="UAT", reporter_id=john.id),
            models.Bug(project_id=proj.id, title="Typo in footer", status="Closed",
                       priority="Low", environment="DEV", reporter_id=admin.id),
        ])
        s.commit()
        s.actor = s.get(models.User, admin.id)
        yield s
    finally:
        s.close()


@pytest.fixture()
def enabled(monkeypatch):
    """Force the cloud layer ON for the test, robustly.

    We mock is_available() directly rather than flipping settings, because
    other suites call get_settings.cache_clear() which would rebuild Settings
    from the real .env mid-test and silently re-disable the layer. Provider
    calls are mocked per-test, so nothing hits the network. is_available() is
    the single gate both try_understand() and the executor consult, so this
    one patch reliably activates the whole path.
    """
    monkeypatch.setattr(cloud_llm, "is_available", lambda: True)


def _gemini_returns(monkeypatch, obj_json: str):
    monkeypatch.setattr(cloud_llm, "_call_gemini", lambda system, user: obj_json)


# ---------------------------------------------------------------------------

def test_disabled_by_default(db, monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "SLEUTH_CLOUD_ENABLED", False)
    assert cloud_llm.is_available() is False
    assert cloud_llm.try_understand("anything at all", db, db.actor) is None


def test_data_question_uses_real_sql_not_a_guess(db, enabled, monkeypatch):
    # Model maps the free-form question to a canonical filter only.
    _gemini_returns(monkeypatch, '{"mode":"data","canonical_query":"open bugs"}')
    cloud_resp = cloud_llm.try_understand("which things are still outstanding?", db, db.actor)
    direct = executor.execute("open bugs", db, db.actor)
    assert cloud_resp is not None
    assert cloud_resp.intent.startswith("cloud_data:")
    # Identical rows to the deterministic rule query → numbers are real.
    cloud_tables = [b for b in cloud_resp.blocks if b.kind == "table"]
    direct_tables = [b for b in direct.blocks if b.kind == "table"]
    assert cloud_tables and direct_tables
    assert cloud_tables[0].payload["rows"] == direct_tables[0].payload["rows"]


def test_cloud_never_writes(db, enabled, monkeypatch):
    bug = db.query(models.Bug).filter_by(title="Crash on login").first()
    assert bug.status == "New"
    # Even if the model emits a write canonical query, it must be dropped.
    _gemini_returns(monkeypatch, f'{{"mode":"data","canonical_query":"close bug {bug.id}"}}')
    resp = cloud_llm.try_understand("can you close the login crash please", db, db.actor)
    assert resp is None                      # dropped, fell through
    db.expire_all()
    assert db.get(models.Bug, bug.id).status == "New"   # unchanged


def test_falls_back_to_openrouter(db, enabled, monkeypatch):
    monkeypatch.setattr(cloud_llm, "_call_gemini", lambda system, user: None)
    monkeypatch.setattr(cloud_llm, "_call_openrouter",
                        lambda system, user: '{"mode":"answer","text":"Bug #1 is the login crash."}')
    resp = cloud_llm.try_understand("explain the login crash", db, db.actor)
    assert resp is not None
    assert resp.intent == "cloud_answer"
    assert "login crash" in resp.blocks[0].payload["text"]


def test_secrets_redacted_before_send(db, enabled, monkeypatch):
    seen = {}

    def capture(system, user):
        seen["user"] = user
        return '{"mode":"unknown"}'

    monkeypatch.setattr(cloud_llm, "_call_gemini", capture)
    cloud_llm.try_understand("my key is AIzaSyABCDEFGHIJKLMNOaBcDeFgHiJkLmNoPqRs", db, db.actor)
    assert "AIzaSy" not in seen["user"]
    assert "[REDACTED]" in seen["user"]


def test_report_person_date_matches_rule_path(db, enabled, monkeypatch):
    # "How many bugs did John close last week?" → the model picks the report
    # filter; the count comes from the same handler the rules use.
    _gemini_returns(
        monkeypatch,
        '{"mode":"data","canonical_query":"report of who solved how many bugs last week"}',
    )
    cloud_resp = cloud_llm.try_understand("how many did John close last week?", db, db.actor)
    direct = executor.execute("report of who solved how many bugs last week", db, db.actor)
    assert cloud_resp is not None
    assert cloud_resp.summary == direct.summary   # identical deterministic output


def test_answer_mode_returns_conversational_text(db, enabled, monkeypatch):
    _gemini_returns(
        monkeypatch,
        '{"mode":"answer","text":"I can\'t add bugs myself, but you can type '
        '\'create bug Login crash\' and confirm it."}',
    )
    resp = cloud_llm.try_understand("can you add bugs?", db, db.actor)
    assert resp is not None and resp.intent == "cloud_answer"
    assert "create bug" in resp.blocks[0].payload["text"].lower()


def test_cloud_preempts_weak_classifier_in_execute(db, enabled, monkeypatch):
    # Regression for the "dumb chatbot" screenshot: a conversational message
    # that the rules don't parse used to be caught by the classifier (a weak
    # guess) before the AI. With cloud ON, execute() must return the AI answer.
    _gemini_returns(monkeypatch,
                    '{"mode":"answer","text":"Happy to help with your bugs!"}')
    resp = executor.execute("be smart, not dumb", db, db.actor)
    assert resp.intent == "cloud_answer"
    assert "help" in resp.blocks[0].payload["text"].lower()


def test_ai_first_beats_greedy_rule_match(db, enabled, monkeypatch):
    # Regression for the "Found 1 admins" screenshot: a conversational
    # permissions question contains the word "admin", which the keyword rules
    # greedily match to list_users. With cloud ON the AI must answer first.
    _gemini_returns(
        monkeypatch,
        '{"mode":"answer","text":"No - only an admin can revoke sessions, '
        'from the Sessions panel. Ask an admin to help."}',
    )
    resp = executor.execute(
        "assume I am not the admin, can you revoke someone's session?", db, db.actor
    )
    assert resp.intent == "cloud_answer"
    assert "admin" in resp.blocks[0].payload["text"].lower()


def test_trivial_greetings_skip_the_cloud(db, enabled, monkeypatch):
    # One-worders keep their instant canned replies even in AI-first mode.
    def boom(system, user):  # pragma: no cover - must never run
        raise AssertionError("cloud should not be called for 'hi'")

    monkeypatch.setattr(cloud_llm, "_call_gemini", boom)
    monkeypatch.setattr(cloud_llm, "_call_openrouter", boom)
    resp = executor.execute("hi", db, db.actor)
    assert resp.intent == "greeting"


def test_history_passed_to_model(db, enabled, monkeypatch):
    # Prior turns are pulled from the transcript and sent so follow-ups have
    # context. Seed a conversation, then assert it reaches the prompt.
    conv = models.ChatConversation(user_id=db.actor.id)
    db.add(conv); db.flush()
    db.add(models.ChatMessage(conversation_id=conv.id, role="user",
                              content="show me critical bugs"))
    db.commit()
    seen = {}

    def capture(system, user):
        seen["user"] = user
        return '{"mode":"answer","text":"ok"}'

    monkeypatch.setattr(cloud_llm, "_call_gemini", capture)
    cloud_llm.try_understand("and the high priority ones?", db, db.actor)
    assert "show me critical bugs" in seen["user"]   # history reached the model
