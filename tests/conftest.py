"""Pytest fixtures: hermetic temp-SQLite app; client (anon), admin_client, user_client."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Hard-set (not setdefault): config.py auto-loads .env, and cloud layers must stay off in CI.
for _flag in (
    "SLEUTH_CLOUD_ENABLED",
    "SLEUTH_RETRIEVAL_ENABLED",
    "SLEUTH_VERIFY_ANSWERS",
    "SLEUTH_AGENT_ENABLED",
    "SLEUTH_EVAL_ENABLED",
    "SLEUTH_RAG_ENABLED",
):
    os.environ[_flag] = "0"

# Digest off: when on, immediate notify_* emails defer to the batch job.
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
    # No real HaveIBeenPwned calls; breach tests monkeypatch app.password_breach.
    monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "false")
    # Push off by default so no real FCM calls; push tests opt in via monkeypatch.
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
    """TestClient logged in as a fresh regular user; separate instance so the admin cookie doesn't bleed over."""
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
    # Same TestClient is fine; the cookie is simply replaced.
    client.post("/api/auth/logout")
    res = client.post("/api/auth/login", json={
        "email": "user@test.local", "password": "User12345",
    })
    assert res.status_code == 200
    return client
