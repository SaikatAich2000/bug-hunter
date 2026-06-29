"""Coverage tests for the CRUD routes: projects, users, sessions, audit, notifications.

Relies entirely on the ``client`` / ``admin_client`` / ``user_client`` fixtures
from conftest.py (temp SQLite, email/push/HIBP disabled). Tests that need to
manipulate persisted rows directly use the app's own SQLAlchemy ``SessionLocal``.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

_BOOTSTRAP_EMAIL = "admin@test.local"
_BOOTSTRAP_PW = "Admin1234"


# Local helpers, kept independent of other test files.
def _login(c: TestClient, email: str, password: str) -> None:
    c.post("/api/auth/logout")
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _as_admin(c: TestClient) -> None:
    _login(c, _BOOTSTRAP_EMAIL, _BOOTSTRAP_PW)


def _mk_project(c: TestClient, name: str, color: str = "#c9764f") -> dict:
    r = c.post("/api/projects", json={"name": name, "color": color})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_user(c: TestClient, name: str, email: str, role: str = "user",
             password: str = "User12345") -> dict:
    r = c.post("/api/users", json={
        "name": name, "email": email, "role": role, "password": password,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _mk_bug(c: TestClient, project_id: int, **extra) -> dict:
    body = {"project_id": project_id, "title": "cov bug",
            "priority": "Medium", "environment": "DEV"}
    body.update(extra)
    r = c.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _new_client() -> TestClient:
    """Return a second TestClient sharing the same app/db, so two users can hold
    independent session cookies simultaneously."""
    from app.main import app
    return TestClient(app)


# ===========================================================================
# projects.py
# ===========================================================================
def test_cov_get_project_by_id_ok(admin_client):
    """GET /api/projects/{id} returns the created row."""
    p = _mk_project(admin_client, "Cov Get OK")
    r = admin_client.get(f"/api/projects/{p['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == p["id"]
    assert body["name"] == "Cov Get OK"


def test_cov_get_project_by_id_404(admin_client):
    """GET a non-existent project id → 404."""
    r = admin_client.get("/api/projects/999999")
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"


def test_cov_update_project_duplicate_name_409(admin_client):
    """PUT that renames a project to an existing name hits the IntegrityError rollback → 409."""
    _mk_project(admin_client, "Cov Dup First")
    second = _mk_project(admin_client, "Cov Dup Second")
    r = admin_client.put(f"/api/projects/{second['id']}",
                         json={"name": "Cov Dup First", "color": "#123abc"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Project name already exists"


def test_cov_update_project_404(admin_client):
    """PUT a missing project → 404."""
    r = admin_client.put("/api/projects/888888", json={"name": "Cov Missing Upd"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"


def test_cov_delete_project_success(admin_client):
    """DELETE a project with no bugs returns the success message and removes the row."""
    p = _mk_project(admin_client, "Cov Del OK")
    r = admin_client.delete(f"/api/projects/{p['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "Project deleted"
    assert admin_client.get(f"/api/projects/{p['id']}").status_code == 404


def test_cov_delete_project_404(admin_client):
    """DELETE a non-existent project → 404."""
    r = admin_client.delete("/api/projects/777777")
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"


def test_cov_delete_project_conflict_when_bugs_exist(admin_client):
    """DELETE is blocked with 409 while bugs belong to the project."""
    p = _mk_project(admin_client, "Cov Del Blocked")
    _mk_bug(admin_client, p["id"], title="blocks delete")
    r = admin_client.delete(f"/api/projects/{p['id']}")
    assert r.status_code == 409
    assert "bug(s) belong to this project" in r.json()["detail"]


# ===========================================================================
# users.py
# ===========================================================================
def test_cov_list_users_exclude_inactive(admin_client):
    """GET /api/users?include_inactive=false omits deactivated users."""
    dormant = _mk_user(admin_client, "Cov Dormant", "cov.dormant@example.com")
    r = admin_client.put(f"/api/users/{dormant['id']}", json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False

    active_ids = {u["id"] for u in admin_client.get(
        "/api/users?include_inactive=false").json()}
    assert dormant["id"] not in active_ids
    # Default (include_inactive=true) still shows them.
    all_ids = {u["id"] for u in admin_client.get("/api/users").json()}
    assert dormant["id"] in all_ids


def test_cov_get_user_by_id_ok(admin_client):
    """GET /api/users/{id} returns the created user."""
    u = _mk_user(admin_client, "Cov GetUser", "cov.getuser@example.com")
    r = admin_client.get(f"/api/users/{u['id']}")
    assert r.status_code == 200
    assert r.json()["email"] == "cov.getuser@example.com"


def test_cov_get_user_by_id_404(admin_client):
    """GET a missing user id → 404."""
    r = admin_client.get("/api/users/424242")
    assert r.status_code == 404
    assert r.json()["detail"] == "User not found"


def test_cov_update_user_cannot_deactivate_self(admin_client):
    """Admin cannot set is_active=false on their own account (self-edit guardrail → 400)."""
    me = admin_client.get("/api/auth/me").json()
    r = admin_client.put(f"/api/users/{me['id']}", json={"is_active": False})
    assert r.status_code == 400
    assert "deactivate yourself" in r.json()["detail"].lower()


def test_cov_update_user_last_admin_demote_self_blocked(admin_client):
    """The sole admin demoting themselves is blocked with 400 (self-edit guard fires
    before the last-admin guardrail; the guardrail raise is unreachable — see note below)."""
    me = admin_client.get("/api/auth/me").json()
    r = admin_client.put(f"/api/users/{me['id']}", json={"role": "user"})
    assert r.status_code == 400
    assert "demote yourself" in r.json()["detail"].lower()


def test_cov_update_user_demote_other_admin_runs_last_admin_check(admin_client):
    """Demoting a different admin (actor != target) skips the self-edit guard and
    runs the last-admin count. The bootstrap actor is still an active admin, so
    the count is >= 1 and the demotion succeeds. Covers the count branch; the
    unreachable raise is documented below."""
    other = _mk_user(admin_client, "Cov Admin3", "cov.admin3@example.com",
                     role="admin", password="Admin3Pass9")
    r = admin_client.put(f"/api/users/{other['id']}", json={"role": "user"})
    assert r.status_code == 200
    assert r.json()["role"] == "user"


def test_cov_update_user_404(admin_client):
    """PUT a missing user id → 404."""
    r = admin_client.put("/api/users/313131", json={"name": "Cov Nope"})
    assert r.status_code == 404
    assert r.json()["detail"] == "User not found"


def test_cov_update_user_duplicate_email_409(admin_client):
    """Updating a user's email to one already in use hits the IntegrityError branch → 409."""
    a = _mk_user(admin_client, "Cov EmailA", "cov.email.a@example.com")
    _mk_user(admin_client, "Cov EmailB", "cov.email.b@example.com")
    r = admin_client.put(f"/api/users/{a['id']}",
                         json={"email": "cov.email.b@example.com"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Email already exists"


def test_cov_update_user_role_change_and_password_reset(admin_client):
    """Promote a user to manager and reset their password in one PUT. 'changeme'
    must still be accepted (legacy exception), so we use it to prove the path works."""
    u = _mk_user(admin_client, "Cov Promote", "cov.promote@example.com")
    r = admin_client.put(f"/api/users/{u['id']}",
                         json={"role": "manager", "password": "changeme"})
    assert r.status_code == 200
    assert r.json()["role"] == "manager"
    # Confirm the new password actually works.
    other_c = _new_client()
    rl = other_c.post("/api/auth/login",
                      json={"email": "cov.promote@example.com", "password": "changeme"})
    assert rl.status_code == 200


def test_cov_delete_user_404(admin_client):
    """DELETE a missing user id → 404."""
    r = admin_client.delete("/api/users/565656")
    assert r.status_code == 404
    assert r.json()["detail"] == "User not found"


def test_cov_delete_user_admin_runs_last_admin_count(admin_client):
    """Deleting an admin (actor != target) runs the last-admin count. The
    bootstrap actor is still active, so count >= 1 and the delete succeeds.
    Covers the count query; the unreachable raise is documented below."""
    target = _mk_user(admin_client, "Cov DelAdmin", "cov.deladmin@example.com",
                      role="admin", password="DelAdmin99")
    r = admin_client.delete(f"/api/users/{target['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "User deleted"
    assert admin_client.get(f"/api/users/{target['id']}").status_code == 404


def test_cov_delete_user_success(admin_client):
    """DELETE a regular user succeeds and returns message."""
    u = _mk_user(admin_client, "Cov DelOK", "cov.delok@example.com")
    r = admin_client.delete(f"/api/users/{u['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "User deleted"
    assert admin_client.get(f"/api/users/{u['id']}").status_code == 404


# ===========================================================================
# sessions.py
# ===========================================================================
def test_cov_sessions_list_and_is_current(admin_client):
    """Session list returns multiple rows and marks exactly the admin's own row
    as is_current."""
    _mk_user(admin_client, "Cov SessU", "cov.sessu@example.com")
    other_c = _new_client()
    _login(other_c, "cov.sessu@example.com", "User12345")

    rows = admin_client.get("/api/sessions").json()
    assert isinstance(rows, list) and len(rows) >= 2
    current = [r for r in rows if r["is_current"]]
    assert len(current) == 1, "exactly one row should be the admin's own session"
    assert current[0]["user_email"] == _BOOTSTRAP_EMAIL


def test_cov_sessions_sweep_expired_rows(admin_client):
    """Listing sessions deletes rows whose expires_at is in the past. We
    back-date a session row via SessionLocal directly, then confirm the
    GET sweeps it."""
    from datetime import datetime, timezone, timedelta
    from app.database import SessionLocal
    from app.models import Session as SessionRow

    _mk_user(admin_client, "Cov Expired", "cov.expired@example.com")
    other_c = _new_client()
    _login(other_c, "cov.expired@example.com", "User12345")

    # Back-date the user's session row so the sweep logic will remove it.
    db = SessionLocal()
    try:
        from app.models import User
        uid = db.query(User).filter(User.email == "cov.expired@example.com").one().id
        row = db.query(SessionRow).filter(SessionRow.user_id == uid).one()
        target_id = row.id
        row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
    finally:
        db.close()

    rows = admin_client.get("/api/sessions").json()
    assert all(r["id"] != target_id for r in rows), "expired row should be swept"

    # Double-check at the DB level.
    db2 = SessionLocal()
    try:
        assert db2.get(SessionRow, target_id) is None
    finally:
        db2.close()


def test_cov_revoke_specific_session(admin_client):
    """Admin revokes another user's session, gets the message, and the row is gone."""
    _mk_user(admin_client, "Cov Revoke", "cov.revoke@example.com")
    other_c = _new_client()
    _login(other_c, "cov.revoke@example.com", "User12345")

    rows = admin_client.get("/api/sessions").json()
    victim = next(r for r in rows if r["user_email"] == "cov.revoke@example.com")
    r = admin_client.delete(f"/api/sessions/{victim['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "Session revoked"
    after = {row["id"] for row in admin_client.get("/api/sessions").json()}
    assert victim["id"] not in after


def test_cov_revoke_own_session_blocked(admin_client):
    """Admin cannot revoke their own current session."""
    rows = admin_client.get("/api/sessions").json()
    mine = next(r for r in rows if r["is_current"])
    r = admin_client.delete(f"/api/sessions/{mine['id']}")
    assert r.status_code == 400
    assert "your own current session" in r.json()["detail"].lower()


def test_cov_revoke_session_404(admin_client):
    """Revoking a non-existent session id → 404."""
    r = admin_client.delete("/api/sessions/909090")
    assert r.status_code == 404
    assert r.json()["detail"] == "Session not found"


# ===========================================================================
# audit.py
# ===========================================================================
def test_cov_audit_filter_by_entity_actor_and_numeric_query(admin_client):
    """Exercises the audit filter/pagination branch: entity_type, actor_user_id,
    numeric free-text (triggers the OR-clause builder for entity_id/bug_id/cast),
    and limit/offset. Only checks that the response is 200 + list; exact rows
    vary by id."""
    me = admin_client.get("/api/auth/me").json()
    p = _mk_project(admin_client, "Cov Audit Proj")
    bug = _mk_bug(admin_client, p["id"], title="Cov Audit Bug")

    # entity_type + actor filter + free-text + numeric (bug id) + pagination.
    r = admin_client.get(
        "/api/audit",
        params={
            "entity_type": "bug",
            "actor_user_id": me["id"],
            "q": f"bug {bug['id']}",
            "limit": 50,
            "offset": 0,
        },
    )
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert any(row.get("bug_id") == bug["id"] or row.get("entity_id") == bug["id"]
               for row in rows), rows


def test_cov_audit_query_with_hash_and_offset(admin_client):
    """'#'-prefixed numeric query with a non-zero offset hits the entity_id
    substring cast clause. Just checks 200 + list."""
    p = _mk_project(admin_client, "Cov Audit Proj 2")
    _mk_bug(admin_client, p["id"], title="Cov Audit Bug 2")
    r = admin_client.get("/api/audit", params={"q": "#1", "limit": 10, "offset": 1})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ===========================================================================
# notifications.py
# ===========================================================================
def test_cov_mark_read_already_read_skips_commit(admin_client):
    """Mark-read on an already-read notification must short-circuit (read_at is not None)
    and still return {'ok': True}. We generate a real notification, read it once, then
    call read again to hit the skip branch."""
    p = _mk_project(admin_client, "Cov Notif Proj")
    bob = _mk_user(admin_client, "Cov Notif Bob", "cov.notif.bob@example.com")
    _mk_bug(admin_client, p["id"], title="Cov Notif Bug", assignee_ids=[bob["id"]])

    bob_c = _new_client()
    _login(bob_c, "cov.notif.bob@example.com", "User12345")
    notifs = bob_c.get("/api/notifications").json()
    assert notifs, "Bob should have an 'assigned' notification"
    nid = notifs[0]["id"]

    r1 = bob_c.post(f"/api/notifications/{nid}/read")
    assert r1.status_code == 200 and r1.json() == {"ok": True}

    # Second call hits the skip branch (read_at already set).
    r2 = bob_c.post(f"/api/notifications/{nid}/read")
    assert r2.status_code == 200 and r2.json() == {"ok": True}

    # Still exactly one row, still marked read.
    row = next(n for n in bob_c.get("/api/notifications").json() if n["id"] == nid)
    assert row["read_at"] is not None


# Intentionally-uncovered lines
#
# A few defensive/dead-code paths can't be reached by any clean, non-brittle
# API test and are left uncovered intentionally:
#
#   sessions.py _is_current — `if not parsed: return False`
#     Unreachable. Every caller is admin-gated, and auth already rejects
#     malformed cookies before the handler runs, so parse_session_token always
#     returns a truthy tuple by the time _is_current is called.
#
#   audit.py — `except ValueError: pass` after int(digits_match...)
#     Unreachable. The captured group comes from re.search(r"\d+", raw),
#     so it is always pure ASCII digits and int() never raises here.
#
#   users.py — last-admin guardrail raise on update and on delete
#     Both raises are pre-empted: actor == target hits the self-edit/self-delete
#     guard first; actor != target means the actor is an active admin, so the
#     other-admin count is always >= 1 and the == 0 branch is never taken.
#     The count queries themselves are covered by
#     test_cov_update_user_demote_other_admin_runs_last_admin_check and
#     test_cov_delete_user_admin_runs_last_admin_count.
