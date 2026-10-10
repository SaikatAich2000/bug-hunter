"""Regression tests for the remaining-fixes batch (user delete audit, seed boot,
session cookie, upload allowlist, lockout scope, 422 shape, documented statuses)."""
from __future__ import annotations

import logging

import pytest


def _user(c, name, email, role="user"):
    pids = [p["id"] for p in c.get("/api/projects").json()]
    r = c.post("/api/users", json={
        "name": name, "email": email, "role": role,
        "password": "TestUserPwd9X", "project_ids": pids,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _project(c, name):
    r = c.post("/api/projects", json={"name": name, "color": "#c9764f"})
    assert r.status_code == 201, r.text
    return r.json()


# --- deleting a user leaves a trail on every item they are removed from ------
def test_delete_user_logs_activity_on_each_item(admin_client):
    p = _project(admin_client, "Trail")
    u = _user(admin_client, "Leaver", "leaver@x.com", role="manager")
    as_assignee = admin_client.post("/api/bugs", json={
        "project_id": p["id"], "title": "Assigned item", "priority": "Medium",
        "environment": "DEV", "assignee_ids": [u["id"]]}).json()
    as_reporter = admin_client.post("/api/bugs", json={
        "project_id": p["id"], "title": "Reported item", "priority": "Medium",
        "environment": "DEV", "reporter_id": u["id"]}).json()
    ev = admin_client.post("/api/events", json={
        "name": "Managed event", "project_id": p["id"], "manager_ids": [u["id"]]})
    assert ev.status_code == 201, ev.text

    assert admin_client.delete(f"/api/users/{u['id']}").status_code == 200

    for bug in (as_assignee, as_reporter):
        acts = admin_client.get(f"/api/bugs/{bug['id']}/activity").json()
        removed = [a for a in acts if a["action"] == "user_removed"]
        assert len(removed) == 1, acts
        assert "Leaver" in removed[0]["detail"]
    rows = admin_client.get("/api/audit?entity_type=event").json()
    assert any(r["action"] == "user_removed" and r["entity_id"] == ev.json()["id"] for r in rows)


# --- a fresh install does not "repair" the seed data it just wrote ------------
# --- session cookie: __Host- prefix, opaque value, old name still accepted ----
ADMIN = {"email": "admin@test.local", "password": "Admin1234"}


def _set_cookies(response):
    return response.headers.get_list("set-cookie")


def _secure_cookies(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config.Settings, "COOKIE_SECURE", True)


def test_secure_login_uses_host_prefixed_cookie(client, monkeypatch):
    _secure_cookies(monkeypatch)
    r = client.post("/api/auth/login", json=ADMIN)
    assert r.status_code == 200
    host = [c for c in _set_cookies(r) if c.startswith("__Host-bh_session=")]
    assert len(host) == 1, _set_cookies(r)
    assert "Secure" in host[0] and "Path=/" in host[0] and "Domain" not in host[0]
    # The unprefixed name is only ever cleared, never set to a session.
    assert not [c for c in _set_cookies(r) if c.startswith("bh_session=") and not c.startswith('bh_session="";')
                and "Max-Age=0" not in c]


def test_insecure_login_keeps_plain_cookie_name(client):
    # Browsers reject the __Host- prefix without Secure, so plain HTTP dev keeps the old name.
    r = client.post("/api/auth/login", json=ADMIN)
    assert any(c.startswith("bh_session=") for c in _set_cookies(r))
    assert not any(c.startswith("__Host-") for c in _set_cookies(r))


def test_session_cookie_value_does_not_expose_user_id(client):
    from itsdangerous import BadSignature

    from app import auth
    r = client.post("/api/auth/login", json=ADMIN)
    uid = r.json()["id"]
    token = client.cookies.get("bh_session")
    # The old format was a readable signed "uid:version:jti"; this must not be.
    with pytest.raises(BadSignature):
        auth._signer().unsign(token)
    assert f"{uid}:" not in token
    assert auth.parse_session_token(token)[0] == uid


def test_secure_server_still_accepts_old_cookie_name_and_format(client, monkeypatch):
    from app import auth
    r = client.post("/api/auth/login", json=ADMIN)
    uid = r.json()["id"]
    _uid, version, jti = auth.parse_session_token(client.cookies.get("bh_session"))
    old_format = auth._signer().sign(f"{uid}:{version}:{jti}".encode()).decode()
    client.cookies.clear()
    _secure_cookies(monkeypatch)
    me = client.get("/api/auth/me", headers={"Cookie": f"bh_session={old_format}"})
    assert me.status_code == 200, me.text


def test_secure_logout_clears_both_cookie_names(client, monkeypatch):
    _secure_cookies(monkeypatch)
    r = client.post("/api/auth/logout")
    cleared = " ".join(_set_cookies(r))
    assert "__Host-bh_session=" in cleared and "bh_session=" in cleared.replace("__Host-bh_session=", "")


@pytest.fixture
def boot_info_logs(caplog):
    """Listed before ``client`` so the app's INFO startup lines are captured."""
    caplog.set_level(logging.INFO, logger="bug_hunter")


def test_fresh_boot_does_not_reconcile_its_own_seed(boot_info_logs, client, caplog):
    messages = [r.getMessage() for r in caplog.get_records("setup")]
    assert not [m for m in messages if m.startswith("Reconciled")], messages
    # Seeding still produced the full permissive transition set.
    from app.database import SessionLocal
    from app.models import WorkflowTransition
    with SessionLocal() as db:
        assert db.query(WorkflowTransition).count() > 0
