"""Pytest fixtures.

Tests run against a temporary SQLite file so they don't need Postgres
running. Same SQLAlchemy models work on both backends.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("API_KEY", "")            # disable auth gate
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")  # silence email service in tests

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
