"""
Layer 3 only activates if a model file is present. The test for it
verifies the *path* (graceful degradation when no model exists) rather
than actually downloading a 350 MB GGUF in CI.
"""
from __future__ import annotations

import os as _os, sys as _sys
# Make the bug-hunter root importable when run directly.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["SESSION_SECRET"] = "test-classifier-and-llm"
os.environ["BOOTSTRAP_ADMIN_EMAIL"] = "admin@example.com"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "AdminPass123!"
os.environ["BOOTSTRAP_ADMIN_NAME"] = "Admin Person"
# Force the LLM model path somewhere that doesn't exist so the LLM
# layer reports unavailable for the duration of these tests.
os.environ["SLEUTH_LLM_MODEL_PATH"] = "/tmp/__sleuth_no_model__.gguf"

# Force a fresh app import bound to this file's dedicated DB (see the note in
# test_sleuth_actions.py) — avoids a full-suite "no such table" from a shared
# engine bound to an earlier-collected module's torn-down DB.
import sys as _sys_purge
for _m in list(_sys_purge.modules):
    if _m == "app" or _m.startswith("app."):
        del _sys_purge.modules[_m]

from app.database import Base, engine, SessionLocal
from app import models
from app.auth import hash_password
from app.chatbot import classifier, executor, llm

import pytest  # noqa: E402  (after the deliberate sys.modules purge above)


@pytest.fixture(autouse=True)
def _rebind_app_modules():
    """Re-bind this file's module-level app.* references to the current import
    generation each test (conftest's `client` fixture purges `app.*` to rebind
    the engine per test; stale refs would otherwise diverge from execute()'s)."""
    import importlib
    g = globals()
    db_mod = importlib.import_module("app.database")
    g["Base"], g["engine"], g["SessionLocal"] = (
        db_mod.Base, db_mod.engine, db_mod.SessionLocal,
    )
    g["models"] = importlib.import_module("app.models")
    g["hash_password"] = importlib.import_module("app.auth").hash_password
    g["classifier"] = importlib.import_module("app.chatbot.classifier")
    g["executor"] = importlib.import_module("app.chatbot.executor")
    g["llm"] = importlib.import_module("app.chatbot.llm")
    yield


PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        raise AssertionError(f"{name}: {detail}")


def section(t):
    print(f"\n=== {t} ===")


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        admin = models.User(name="Admin Person", email="admin@example.com",
                            role="admin",
                            password_hash=hash_password("AdminPass123!"),
                            is_active=True)
        alice = models.User(name="Alice", email="alice@example.com",
                            role="manager",
                            password_hash=hash_password("x"),
                            is_active=True)
        db.add_all([admin, alice])
        db.commit()
        proj = models.Project(name="Apollo")
        db.add(proj); db.commit()
        bugs = [
            models.Bug(title="Login broken", description="d",
                       status="New", priority="High", environment="PROD",
                       project_id=proj.id, reporter_id=admin.id),
            models.Bug(title="Crash on save", description="d",
                       status="Closed", priority="Low", environment="DEV",
                       project_id=proj.id, reporter_id=admin.id),
        ]
        db.add_all(bugs); db.commit()
        return admin.id
    finally:
        db.close()


def test_classifier_basic():
    section("Classifier predictions")
    cases = [
        ("anyone there", "greeting"),
        ("ty mate", "thanks"),
        ("show me the dashboard", "stats"),
        ("what's still open", "list_bugs"),
        ("team list", "list_users"),
        ("kpi please", "stats"),
        ("project list", "list_projects"),
        ("what was changed", "recent_activity"),
        ("guide me", "help"),
        ("let bob handle bug 5", "action_assign"),
        ("this one is fixed", "action_set_status"),
        ("log a bug for the date filter", "action_create_bug"),
    ]
    for msg, expected in cases:
        p = classifier.predict(msg)
        ok = p is not None and p.intent == expected
        detail = "" if ok else f"got {p}"
        check(f"clf: {msg!r} -> {expected}", ok, detail)

    # Below-threshold cases
    nonsense = [
        "blah blah xyzzy",
        "asdfghjkl qwerty",
        "🦄🦄🦄",
    ]
    for msg in nonsense:
        p = classifier.predict(msg)
        # Either None or low confidence is acceptable
        ok = (p is None) or (p.confidence < 0.5)
        check(f"clf: nonsense {msg!r} -> low/None",
              ok, f"got {p}")


def test_classifier_via_executor():
    section("Classifier as executor fallback")
    admin_id = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)

        # A phrasing the rule parser will miss but the classifier knows
        # — "anyone there" looks like a greeting in the corpus.
        # The rule parser has its own greeting matcher, so to actually
        # exercise the classifier path we need a phrase the rules don't
        # match. "what was changed" is in the corpus for recent_activity
        # and the rules DO have a recent-activity matcher — so let's
        # use a paraphrase the rules don't catch.
        resp = executor.execute("show me the dashboard", db, admin)
        check("'show me the dashboard' routed to stats",
              resp.intent == "stats", f"got {resp.intent}")

        resp = executor.execute("kpi please", db, admin)
        check("'kpi please' routed to stats (rule path)",
              resp.intent == "stats", f"got {resp.intent}")

        # A query that only the classifier should catch:
        #   "team list" — rules want literal "user/users/admin" or "list",
        #   classifier corpus has "team list" -> list_users.
        resp = executor.execute("team list", db, admin)
        # Rules might still catch "list", routing to list_bugs. Either
        # list_users (classifier win) or list_bugs (rule path) is OK
        # here; what matters is we don't return "unknown".
        check("'team list' is handled (not unknown)",
              resp.intent in ("list_users", "list_bugs"),
              f"got {resp.intent}")
    finally:
        db.close()


def test_llm_unavailable_path():
    section("LLM layer: unavailable when no model file")
    check("llm.is_available() is False without GGUF",
          llm.is_available() is False)
    # Calling try_understand should return None, not crash.
    db = SessionLocal()
    try:
        admin = db.query(models.User).first()
        if admin is None:
            seed()
            admin = db.query(models.User).first()
        result = llm.try_understand("some weird query", db, admin)
        check("llm.try_understand returns None when unavailable",
              result is None)
        # No model file → no budget, no shortfall message
        check("memory_budget is None without GGUF",
              llm.memory_budget() is None)
        check("memory_shortfall_message is None without GGUF",
              llm.memory_shortfall_message() is None)
    finally:
        db.close()


def test_llm_memory_budget_calculation():
    section("LLM memory budget: detects undersized hardware")
    import tempfile
    # Stage a fake 400 MB "model" file.
    fake = tempfile.NamedTemporaryFile(suffix=".gguf", delete=False)
    fake.seek(400 * 1024 * 1024 - 1)
    fake.write(b"\x00")
    fake.close()
    old_path = llm._MODEL_PATH
    from pathlib import Path as _P
    llm._MODEL_PATH = _P(fake.name)

    # Patch the memory-detection helpers to simulate an undersized box.
    orig_avail = llm._detect_available_mb
    orig_cg = llm._detect_container_limit_mb
    llm._detect_available_mb = lambda: 350
    llm._detect_container_limit_mb = lambda: 512
    try:
        budget = llm.memory_budget()
        check("budget: model size detected at 400 MB",
              budget is not None and budget.model_size_mb == 400,
              f"got {budget}")
        check("budget: estimated_need > available (insufficient)",
              budget is not None and not budget.sufficient,
              f"got {budget}")
        check("budget: container_limit_mb == 512",
              budget is not None and budget.container_limit_mb == 512)

        # Shortfall message is now a single user-friendly line; the
        # detailed operator info goes to logs via is_available().
        msg = llm.memory_shortfall_message()
        check("shortfall: message exists when budget insufficient",
              msg is not None)
        check("shortfall: message is short (single line, < 200 chars)",
              msg is not None and len(msg) < 200, f"len={len(msg) if msg else 0}")
        check("shortfall: mentions 'unavailable' (user-friendly)",
              msg is not None and "unavailable" in msg.lower(), msg)
        check("shortfall: does NOT leak operator detail to user",
              msg is not None and "docker-compose" not in msg.lower()
              and "MB" not in msg,
              "operator detail leaking into user message")

        # is_available should refuse load when budget is insufficient.
        check("is_available: False when RAM too low",
              llm.is_available() is False)
    finally:
        llm._detect_available_mb = orig_avail
        llm._detect_container_limit_mb = orig_cg
        llm._MODEL_PATH = old_path
        os.unlink(fake.name)


def test_llm_memory_budget_sufficient():
    section("LLM memory budget: 'enough RAM' path")
    import tempfile
    # 100 MB model on a box with 1500 MB available — should be sufficient.
    fake = tempfile.NamedTemporaryFile(suffix=".gguf", delete=False)
    fake.seek(100 * 1024 * 1024 - 1)
    fake.write(b"\x00")
    fake.close()
    old_path = llm._MODEL_PATH
    from pathlib import Path as _P
    llm._MODEL_PATH = _P(fake.name)
    orig_avail = llm._detect_available_mb
    llm._detect_available_mb = lambda: 1500
    try:
        budget = llm.memory_budget()
        check("budget-ok: sufficient flag True",
              budget is not None and budget.sufficient,
              f"got {budget}")
        # No shortfall message when budget is fine.
        check("budget-ok: no shortfall msg",
              llm.memory_shortfall_message() is None)
    finally:
        llm._detect_available_mb = orig_avail
        llm._MODEL_PATH = old_path
        os.unlink(fake.name)


def test_executor_falls_through_simply():
    section("Executor: shortfall stays in logs, user gets simple reply")
    import tempfile
    # Stage a fake model file too big to fit. The rules + classifier
    # should miss our nonsense query, so the executor falls through to
    # Layer 3 — but Layer 3 detects the shortfall, logs it for the
    # operator, and returns None. The user sees the same friendly
    # "didn't understand" reply as if no model existed at all.
    fake = tempfile.NamedTemporaryFile(suffix=".gguf", delete=False)
    fake.seek(400 * 1024 * 1024 - 1)
    fake.write(b"\x00")
    fake.close()
    old_path = llm._MODEL_PATH
    from pathlib import Path as _P
    llm._MODEL_PATH = _P(fake.name)
    orig_avail = llm._detect_available_mb
    orig_cg = llm._detect_container_limit_mb
    llm._detect_available_mb = lambda: 350
    llm._detect_container_limit_mb = lambda: 512
    # Reset the "warned once" flag so this test starts fresh.
    llm._shortfall_warned = False
    try:
        seed()
        db = SessionLocal()
        try:
            admin = db.query(models.User).filter_by(role="admin").one()
            resp = executor.execute(
                "qzlmqop frobnicate xyzzy ineffable", db, admin
            )
            # Falls through to the unknown handler — same friendly
            # response as a normal rules+classifier miss.
            check("exec: user sees a normal 'unknown' reply, not tech text",
                  resp.intent in ("unknown", "fallback")
                  or any(b.kind == "text" for b in resp.blocks),
                  f"got {resp.intent}")
            text = " ".join(b.payload.get("text", "")
                            for b in resp.blocks if b.kind == "text")
            check("exec: NO mention of docker-compose to user",
                  "docker-compose" not in text.lower(),
                  text[:200])
            check("exec: NO mention of MB / RAM details to user",
                  "MB" not in text and "memory" not in text.lower(),
                  text[:200])
            # And is_available() must have flipped the warned-once flag.
            check("exec: operator warning flag was set",
                  llm._shortfall_warned is True)
        finally:
            db.close()
    finally:
        llm._detect_available_mb = orig_avail
        llm._detect_container_limit_mb = orig_cg
        llm._MODEL_PATH = old_path
        os.unlink(fake.name)


def test_unknown_still_returns_helpful_text():
    section("Genuinely unknown queries → fallback text, no crash")
    seed()
    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").one()
        resp = executor.execute("xqzlmqop nonsense gibberish", db, admin)
        check("unknown query — has text block",
              any(b.kind == "text" for b in resp.blocks))
        check("unknown query — does not crash", True)
    finally:
        db.close()


if __name__ == "__main__":
    try:
        test_classifier_basic()
        test_classifier_via_executor()
        test_llm_unavailable_path()
        test_llm_memory_budget_calculation()
        test_llm_memory_budget_sufficient()
        test_executor_falls_through_simply()
        test_unknown_still_returns_helpful_text()
    except Exception:
        traceback.print_exc()
        FAILED.append(("HARNESS", "uncaught"))

    print(f"\n=== RESULTS ===")
    print(f"Passed: {len(PASSED)}")
    print(f"Failed: {len(FAILED)}")
    if FAILED:
        for n, d in FAILED:
            print(f"  FAIL  {n}  {d}")
        sys.exit(1)
    print("All checks passed")
    sys.exit(0)
