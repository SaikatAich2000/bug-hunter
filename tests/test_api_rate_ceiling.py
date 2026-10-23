"""The general per-IP ceiling on /api: it stops a flood, exempts health, and leaves other paths alone."""
from __future__ import annotations

import pytest


@pytest.fixture
def ceiling(monkeypatch):
    import app.main as main

    main._rate_buckets.clear()
    monkeypatch.setattr(main, "_API_RULE", (5, 60))
    yield main
    main._rate_buckets.clear()


def test_api_flood_is_cut_off_with_retry_after(client, ceiling):
    codes = [client.get("/api/auth/me").status_code for _ in range(5)]
    assert 429 not in codes
    blocked = client.get("/api/auth/me")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert "slow down" in blocked.json()["detail"]


def test_health_and_non_api_paths_are_exempt(client, ceiling):
    for _ in range(12):
        assert client.get("/api/health").status_code == 200
    for _ in range(12):
        assert client.get("/login.html").status_code != 429


def test_window_resets_per_client_ip(client, ceiling, monkeypatch):
    for _ in range(6):
        client.get("/api/auth/me")
    assert client.get("/api/auth/me").status_code == 429
    monkeypatch.setattr(ceiling, "_client_ip", lambda request: "203.0.113.9")
    assert client.get("/api/auth/me").status_code != 429


def test_zero_turns_the_ceiling_off(client, monkeypatch):
    import app.main as main

    main._rate_buckets.clear()
    monkeypatch.setattr(main, "_API_RULE", None)
    assert all(client.get("/api/auth/me").status_code != 429 for _ in range(30))
    main._rate_buckets.clear()
