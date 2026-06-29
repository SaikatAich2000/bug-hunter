"""Pytest fixtures.

Tests run against a temporary SQLite file — no Postgres needed. The same
SQLAlchemy models work on both backends.

Three client fixtures are provided:
  - client          : unauthenticated, for testing 401 behaviour.
  - admin_client    : logged in as the bootstrap admin.
  - user_client     : logged in as a regular (non-admin) user.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Disable Sleuth's optional/cloud layers for the whole suite. config.py
# auto-loads .env, so we hard-set (not setdefault) to prevent a local .env
# from enabling them in CI. Tests that exercise these paths opt in by
# monkeypatching the settings object directly.
for _flag in (
    "SLEUTH_CLOUD_ENABLED",
    "SLEUTH_RETRIEVAL_ENABLED",
    "SLEUTH_VERIFY_ANSWERS",
    "SLEUTH_AGENT_ENABLED",
    "SLEUTH_EVAL_ENABLED",
    "SLEUTH_RAG_ENABLED",
):
    os.environ[_flag] = "0"

# Disable the email digest. When it's on, immediate notify_* calls defer to
# the batch job instead of sending, which breaks tests that assert on those
# immediate emails (test_item_types / test_events). Digest-specific tests opt
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
    # Keep the suite hermetic — no real HaveIBeenPwned call. Tests that cover
    # the breach path monkeypatch app.password_breach directly.
    monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "false")
    # Web push off by default — a deployment .env may have WEB_PUSH_ENABLED=true,
    # but we never want real FCM calls in tests. Tests that need push enabled
    # turn it on via monkeypatch.
    monkeypatch.setenv("WEB_PUSH_ENABLED", "false")

    # Re-import so the SQLAlchemy engine picks up the overridden DATABASE_URL.
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
    # Log in as admin and create the regular user.
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
    # Log out, then log in as the regular user (same TestClient is fine;
    # the cookie is simply replaced).
    client.post("/api/auth/logout")
    res = client.post("/api/auth/login", json={
        "email": "user@test.local", "password": "User12345",
    })
    assert res.status_code == 200
    return client
