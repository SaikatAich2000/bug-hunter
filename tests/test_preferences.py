"""Onboarding preferences: stored per user on the server, scoped to the caller."""
from __future__ import annotations

URL = "/api/me/preferences"


def test_requires_login(client):
    assert client.get(URL).status_code == 401
    assert client.put(URL, json={"welcome_seen": True}).status_code == 401


def test_defaults_then_partial_merge(admin_client):
    assert admin_client.get(URL).json() == {
        "welcome_seen": False, "welcome_hidden": False, "tour_status": "none", "tour_step": 0,
    }
    r = admin_client.put(URL, json={"welcome_seen": True, "tour_status": "running", "tour_step": 2})
    assert r.status_code == 200
    r = admin_client.put(URL, json={"welcome_hidden": True})
    assert r.json() == {
        "welcome_seen": True, "welcome_hidden": True, "tour_status": "running", "tour_step": 2,
    }
    assert admin_client.get(URL).json()["tour_step"] == 2


def test_rejects_bad_values(admin_client):
    assert admin_client.put(URL, json={"tour_status": "bogus"}).status_code == 422
    assert admin_client.put(URL, json={"tour_step": -1}).status_code == 422
    assert admin_client.put(URL, json={"user_id": 5}).status_code == 422


def test_state_is_per_user(client, admin_client):
    admin_client.put(URL, json={"welcome_seen": True})
    client.post("/api/users", json={
        "name": "Other", "email": "other@test.local", "role": "user", "password": "User12345",
    })
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={
        "email": "other@test.local", "password": "User12345"}).status_code == 200
    assert client.get(URL).json()["welcome_seen"] is False
