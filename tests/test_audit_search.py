"""Audit search and forgot-password tests.

Audit search must match on bug numbers (entity_id), entity types, and actor
names, not just action/detail/actor_name.

Forgot-password validates emails against the DB. By default unknown addresses
get 204 (enumeration-safe), but the product owner added an opt-out flag that
restores the friendlier 404 for unknown addresses.
"""
from __future__ import annotations


def _seed_audit_data(admin_client):
    """Drop a couple of bugs in so we have predictable audit rows."""
    proj = admin_client.post("/api/projects",
                             json={"name": "Apollo", "color": "#ffffff"}).json()
    bug1 = admin_client.post("/api/bugs", json={
        "title": "Login broken",
        "project_id": proj["id"],
        "priority": "High",
        "environment": "PROD",
    }).json()
    bug2 = admin_client.post("/api/bugs", json={
        "title": "Crash on save",
        "project_id": proj["id"],
        "priority": "Critical",
        "environment": "UAT",
    }).json()
    return bug1, bug2


def test_audit_search_by_bug_number(admin_client):
    bug1, _bug2 = _seed_audit_data(admin_client)
    # Bug creation produces an Activity with entity_id = bug.id; search should hit it.
    rows = admin_client.get(f"/api/audit?q={bug1['id']}").json()
    assert any(r.get("entity_id") == bug1["id"] for r in rows), \
        f"Expected audit row for bug #{bug1['id']} in {rows}"


def test_audit_search_with_hash_prefix(admin_client):
    bug1, _ = _seed_audit_data(admin_client)
    # A "#<id>" prefix should still resolve to the same bug row.
    rows = admin_client.get(f"/api/audit?q=%23{bug1['id']}").json()
    assert any(r.get("entity_id") == bug1["id"] for r in rows)


def test_audit_search_by_entity_type(admin_client):
    _seed_audit_data(admin_client)
    rows = admin_client.get("/api/audit?q=bug").json()
    assert len(rows) >= 2
    # Every returned row should relate to bugs.
    assert all(
        (r.get("entity_type") == "bug") or ("bug" in (r.get("action") or "").lower())
        for r in rows
    )


def test_audit_search_keeps_existing_behaviour(admin_client):
    _seed_audit_data(admin_client)
    # Searching by actor name should still work.
    rows = admin_client.get("/api/audit?q=Test").json()  # admin name is "Test Admin"
    assert any("test admin" in (r.get("actor_name") or "").lower() for r in rows)


def test_forgot_password_known_email_returns_204(client):
    r = client.post("/api/auth/forgot-password",
                    json={"email": "admin@test.local"})
    assert r.status_code == 204


def test_forgot_password_unknown_email_returns_204_by_default(client):
    """Enumeration-safe default: unknown emails get the SAME 204 as known ones."""
    r = client.post("/api/auth/forgot-password",
                    json={"email": "ghost@example.com"})
    assert r.status_code == 204


def test_forgot_password_inactive_email_returns_204_by_default(admin_client):
    """Inactive accounts should not receive reset emails. Under the enumeration-safe
    default, the response is still 204, same as any other address."""
    # Create a user and immediately deactivate them.
    u = admin_client.post("/api/users", json={
        "name": "Inactive", "email": "off@test.local",
        "role": "user", "password": "Inactive99",
    }).json()
    admin_client.put(f"/api/users/{u['id']}", json={
        "name": "Inactive", "email": "off@test.local",
        "role": "user", "is_active": False,
    })
    # Log out so we hit the public endpoint as an anonymous caller.
    admin_client.post("/api/auth/logout")
    r = admin_client.post("/api/auth/forgot-password",
                          json={"email": "off@test.local"})
    assert r.status_code == 204


def test_forgot_password_enumeration_optout_returns_404(client, monkeypatch):
    """FORGOT_PASSWORD_ENUMERATION_SAFE=False restores the friendlier (but leakier)
    legacy behaviour: unknown addresses get a 404. We patch the live Settings class
    so the route (which calls get_settings() per request) picks up the change.
    """
    import app.config as config
    monkeypatch.setattr(config.Settings, "FORGOT_PASSWORD_ENUMERATION_SAFE", False)
    r = client.post("/api/auth/forgot-password",
                    json={"email": "ghost@example.com"})
    assert r.status_code == 404
    assert "couldn't find" in r.json()["detail"].lower()
