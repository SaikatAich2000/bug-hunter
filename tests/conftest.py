"""Pytest fixtures.

Tests run against a temporary SQLite file so they don't need Postgres
running. Same SQLAlchemy models work on both backends.

Three client fixtures:
  - client          : raw, unauthenticated. For testing 401 behaviour.
  - admin_client    : logged in as the bootstrap admin.
  - user_client     : logged in as a regular user (created by admin in fixture).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Force Sleuth's optional/cloud layers OFF for the whole suite, even if a
# developer's local .env enables them (config.py auto-loads .env). Tests that
# exercise these paths opt in by monkeypatching the settings object, so the
# suite never makes a real network call and behaves identically on every
# machine. Hard-set (not setdefault) so a stray .env can't turn them on in CI.
for _flag in (
    "SLEUTH_CLOUD_ENABLED",
    "SLEUTH_RETRIEVAL_ENABLED",
    "SLEUTH_VERIFY_ANSWERS",
    "SLEUTH_AGENT_ENABLED",
    "SLEUTH_EVAL_ENABLED",
    "SLEUTH_RAG_ENABLED",
):
    os.environ[_flag] = "0"

# Force the email digest OFF regardless of a developer's local .env (which may
# set EMAIL_DIGEST_ENABLED=true). With the digest on, the immediate notify_*
# email functions defer to the batch job instead of sending, so the unit tests
# that assert on the immediate emails (test_item_types / test_events) would see
# nothing delivered. Hard-set like the Sleuth flags above; the digest tests opt
# back in per-test via monkeypatch.setattr(get_settings(), ...).
os.environ["EMAIL_DIGEST_ENABLED"] = "false"


BOOTSTRAP_EMAIL = "admin@test.local"
BOOTSTRAP_PASSWORD = "Admin1234"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("SESSION_SECRET", "test_secret_for_tests_only")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", BOOTSTRAP_EMAIL)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", BOOTSTRAP_PASSWORD)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", "Test Admin")
    # Disable the HaveIBeenPwned API call so the suite stays hermetic. The
    # test_security.py cases that exercise the breach path monkeypatch
    # app.password_breach directly instead of making real network calls.
    monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "false")
    # Web push off by default so the suite is hermetic regardless of the
    # deployment .env (which may have WEB_PUSH_ENABLED=true) and never makes a
    # real FCM call. Tests that need it on enable it explicitly via monkeypatch.
    monkeypatch.setenv("WEB_PUSH_ENABLED", "false")

    # Force re-import so the engine picks up the env-var override.
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]

    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin_client(client):
    """A TestClient with an authenticated admin session cookie."""
    res = client.post("/api/auth/login", json={
        "email": BOOTSTRAP_EMAIL,
        "password": BOOTSTRAP_PASSWORD,
    })
    assert res.status_code == 200, f"admin login failed: {res.text}"
    return client


@pytest.fixture()
def user_client(client):
    """A TestClient logged in as a freshly-created regular user.

    Uses a *separate* TestClient instance so the admin's session cookie
    doesn't bleed into the user's session.
    """
    # Step 1: log in as admin (in same client) and create the user.
    res = client.post("/api/auth/login", json={
        "email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD,
    })
    assert res.status_code == 200
    res = client.post("/api/users", json={
        "name": "Regular User",
        "email": "user@test.local",
        "role": "user",
        "password": "User12345",
    })
    assert res.status_code == 201, res.text
    # Step 2: log out, then log in as the user (same TestClient is fine —
    # cookie just gets replaced).
    client.post("/api/auth/logout")
    res = client.post("/api/auth/login", json={
        "email": "user@test.local", "password": "User12345",
    })
    assert res.status_code == 200
    return client
