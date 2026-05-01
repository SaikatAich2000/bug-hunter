"""Pytest fixtures.

Tests run against a temporary SQLite file so they don't need Postgres
running. Same SQLAlchemy models work on both backends.

Three client fixtures:
  - client          : raw, unauthenticated. For testing 401 behaviour.
  - admin_client    : logged in as the bootstrap admin.
  - user_client     : logged in as a regular user (created by admin in fixture).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
