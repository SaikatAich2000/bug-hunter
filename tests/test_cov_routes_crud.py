"""Coverage slice: CRUD routes (projects / users / sessions / audit / notifications).

This file ONLY adds tests — it never edits app source or any other test. Every
test name is prefixed ``test_cov_`` and is unique. The suite is hermetic: it
relies entirely on the ``client`` / ``admin_client`` / ``user_client`` fixtures
from conftest.py (temp SQLite, email/push/HIBP disabled), and the few tests that
need to manipulate persisted rows (expired-session sweep) do so through the
app's own SQLAlchemy ``SessionLocal`` — no real network.

Targets the previously-uncovered lines in:
  app/routes/projects.py, users.py, sessions.py, audit.py, notifications.py
See the per-test docstrings for the exact line(s) each exercises.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

_BOOTSTRAP_EMAIL = "admin@test.local"
_BOOTSTRAP_PW = "Admin1234"


# ---------------------------------------------------------------------------
# Small local helpers (kept independent of other test files' helpers)
# ---------------------------------------------------------------------------
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
    """A second TestClient bound to the same already-imported app/db, so two
    users can hold independent session cookies at once."""
    from app.main import app
    return TestClient(app)


# ===========================================================================
# projects.py
# ===========================================================================
def test_cov_get_project_by_id_ok(admin_client):
    """projects.py 74,77 — GET /api/projects/{id} happy path returns the row."""
    p = _mk_project(admin_client, "Cov Get OK")
    r = admin_client.get(f"/api/projects/{p['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == p["id"]
    assert body["name"] == "Cov Get OK"


def test_cov_get_project_by_id_404(admin_client):
    """projects.py 75-76 — GET a non-existent project id → 404."""
    r = admin_client.get("/api/projects/999999")
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"


def test_cov_update_project_duplicate_name_409(admin_client):
    """projects.py 99-101 — PUT renaming a project onto an existing name hits
    the IntegrityError rollback branch → 409."""
    _mk_project(admin_client, "Cov Dup First")
    second = _mk_project(admin_client, "Cov Dup Second")
    r = admin_client.put(f"/api/projects/{second['id']}",
                         json={"name": "Cov Dup First", "color": "#123abc"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Project name already exists"


def test_cov_update_project_404(admin_client):
    """projects.py 89 (404 guard on update) — PUT a missing project → 404."""
    r = admin_client.put("/api/projects/888888", json={"name": "Cov Missing Upd"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"


def test_cov_delete_project_success(admin_client):
    """projects.py 128-132 — DELETE a project with no bugs runs the success
    path (name snapshot, delete, audit, commit) and returns the message."""
    p = _mk_project(admin_client, "Cov Del OK")
    r = admin_client.delete(f"/api/projects/{p['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "Project deleted"
    # And it's really gone.
    assert admin_client.get(f"/api/projects/{p['id']}").status_code == 404


def test_cov_delete_project_404(admin_client):
    """projects.py 118 — DELETE a non-existent project → 404."""
    r = admin_client.delete("/api/projects/777777")
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"


def test_cov_delete_project_conflict_when_bugs_exist(admin_client):
    """projects.py 123-126 — DELETE is blocked with 409 while bugs belong to
    the project (this is the guard right before the 128-132 success path)."""
    p = _mk_project(admin_client, "Cov Del Blocked")
    _mk_bug(admin_client, p["id"], title="blocks delete")
    r = admin_client.delete(f"/api/projects/{p['id']}")
    assert r.status_code == 409
    assert "bug(s) belong to this project" in r.json()["detail"]


# ===========================================================================
# users.py
# ===========================================================================
def test_cov_list_users_exclude_inactive(admin_client):
    """users.py 81 — GET /api/users?include_inactive=false filters out the
    deactivated user via the is_active branch."""
    dormant = _mk_user(admin_client, "Cov Dormant", "cov.dormant@example.com")
    # Deactivate them.
    r = admin_client.put(f"/api/users/{dormant['id']}", json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False

    active_ids = {u["id"] for u in admin_client.get(
        "/api/users?include_inactive=false").json()}
    assert dormant["id"] not in active_ids
    # Default (include_inactive=true) still shows them.
    all_ids = {u["id"] for u in admin_client.get("/api/users").json()}
    assert dormant["id"] in all_ids


def test_cov_get_user_by_id_ok(admin_client):
    """users.py 139 — GET /api/users/{id} happy path returns the row."""
    u = _mk_user(admin_client, "Cov GetUser", "cov.getuser@example.com")
    r = admin_client.get(f"/api/users/{u['id']}")
    assert r.status_code == 200
    assert r.json()["email"] == "cov.getuser@example.com"


def test_cov_get_user_by_id_404(admin_client):
    """users.py 137-138 — GET a missing user id → 404."""
    r = admin_client.get("/api/users/424242")
    assert r.status_code == 404
    assert r.json()["detail"] == "User not found"


def test_cov_update_user_cannot_deactivate_self(admin_client):
    """users.py 163-164 — admin trying to set is_active=false on their OWN
    account is rejected with 400 (self-edit guardrail)."""
    me = admin_client.get("/api/auth/me").json()
    r = admin_client.put(f"/api/users/{me['id']}", json={"is_active": False})
    assert r.status_code == 400
    assert "deactivate yourself" in r.json()["detail"].lower()


def test_cov_update_user_last_admin_demote_self_blocked(admin_client):
    """users.py self-edit guardrails (161-162) — the sole admin demoting THEMSELVES
    to user is blocked with 400. This is the reachable form of the last-admin
    protection on update: the self-edit guard fires before _check_last_admin_guardrail.

    Why not target the 178 raise directly? See the module note at the bottom of
    this file — line 178 is unreachable behind the self-edit guards and the
    actor-counts-as-an-other-admin arithmetic.
    """
    me = admin_client.get("/api/auth/me").json()
    r = admin_client.put(f"/api/users/{me['id']}", json={"role": "user"})
    assert r.status_code == 400
    assert "demote yourself" in r.json()["detail"].lower()


def test_cov_update_user_demote_other_admin_runs_last_admin_check(admin_client):
    """users.py 173-177 — demoting a DIFFERENT admin (actor != target) skips the
    self-edit guard and runs the last-admin count query. Another active admin
    (the bootstrap actor) remains, so the count is >=1 and the demotion succeeds.
    This exercises the count branch without hitting the unreachable 178 raise."""
    other = _mk_user(admin_client, "Cov Admin3", "cov.admin3@example.com",
                     role="admin", password="Admin3Pass9")
    r = admin_client.put(f"/api/users/{other['id']}", json={"role": "user"})
    assert r.status_code == 200
    assert r.json()["role"] == "user"


def test_cov_update_user_404(admin_client):
    """users.py 215 — PUT a missing user id → 404."""
    r = admin_client.put("/api/users/313131", json={"name": "Cov Nope"})
    assert r.status_code == 404
    assert r.json()["detail"] == "User not found"


def test_cov_update_user_duplicate_email_409(admin_client):
    """users.py 236-238 — updating a user's email to one already taken hits the
    IntegrityError branch → 409."""
    a = _mk_user(admin_client, "Cov EmailA", "cov.email.a@example.com")
    _mk_user(admin_client, "Cov EmailB", "cov.email.b@example.com")
    r = admin_client.put(f"/api/users/{a['id']}",
                         json={"email": "cov.email.b@example.com"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Email already exists"


def test_cov_update_user_role_change_and_password_reset(admin_client):
    """users.py field-change + admin password-reset path: promote a user to
    manager and reset their password in one PUT. 'changeme' MUST be accepted
    (legacy exception) so we use it for the reset to prove it stays valid."""
    u = _mk_user(admin_client, "Cov Promote", "cov.promote@example.com")
    r = admin_client.put(f"/api/users/{u['id']}",
                         json={"role": "manager", "password": "changeme"})
    assert r.status_code == 200
    assert r.json()["role"] == "manager"
    # The reset password ('changeme') really works for login.
    other_c = _new_client()
    rl = other_c.post("/api/auth/login",
                      json={"email": "cov.promote@example.com", "password": "changeme"})
    assert rl.status_code == 200


def test_cov_delete_user_404(admin_client):
    """users.py 256 — DELETE a missing user id → 404."""
    r = admin_client.delete("/api/users/565656")
    assert r.status_code == 404
    assert r.json()["detail"] == "User not found"


def test_cov_delete_user_admin_runs_last_admin_count(admin_client):
    """users.py 262-266 — deleting an admin target (actor != target) executes
    the admin-branch last-admin count query. Another active admin (the bootstrap
    actor) remains, so the count is >=1, line 266's `if` is False, and the delete
    succeeds. This covers the count query; the 267 raise is unreachable (see the
    module note at the bottom of this file)."""
    target = _mk_user(admin_client, "Cov DelAdmin", "cov.deladmin@example.com",
                      role="admin", password="DelAdmin99")
    r = admin_client.delete(f"/api/users/{target['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "User deleted"
    assert admin_client.get(f"/api/users/{target['id']}").status_code == 404


def test_cov_delete_user_success(admin_client):
    """users.py 272-276 — DELETE a regular user succeeds and returns message."""
    u = _mk_user(admin_client, "Cov DelOK", "cov.delok@example.com")
    r = admin_client.delete(f"/api/users/{u['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "User deleted"
    assert admin_client.get(f"/api/users/{u['id']}").status_code == 404


# ===========================================================================
# sessions.py
# ===========================================================================
def test_cov_sessions_list_and_is_current(admin_client):
    """sessions.py list happy path + _is_current True for the admin's own row.
    Also creates a 2nd user's session so the list has >1 row."""
    _mk_user(admin_client, "Cov SessU", "cov.sessu@example.com")
    other_c = _new_client()
    _login(other_c, "cov.sessu@example.com", "User12345")

    rows = admin_client.get("/api/sessions").json()
    assert isinstance(rows, list) and len(rows) >= 2
    current = [r for r in rows if r["is_current"]]
    assert len(current) == 1, "exactly one row should be the admin's own session"
    assert current[0]["user_email"] == _BOOTSTRAP_EMAIL


def test_cov_sessions_sweep_expired_rows(admin_client):
    """sessions.py 64-66 — listing sessions sweeps (deletes) rows whose
    expires_at is in the past. We back-date a user's session row directly via
    the app's own SessionLocal, then assert the GET removes it."""
    from datetime import datetime, timezone, timedelta
    from app.database import SessionLocal
    from app.models import Session as SessionRow

    _mk_user(admin_client, "Cov Expired", "cov.expired@example.com")
    other_c = _new_client()
    _login(other_c, "cov.expired@example.com", "User12345")

    # Find that user's freshly-created session row and back-date it.
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

    # Listing triggers the sweep branch; the expired row must be gone.
    rows = admin_client.get("/api/sessions").json()
    assert all(r["id"] != target_id for r in rows), "expired row should be swept"

    # Confirm at the DB level too.
    db2 = SessionLocal()
    try:
        assert db2.get(SessionRow, target_id) is None
    finally:
        db2.close()


def test_cov_revoke_specific_session(admin_client):
    """sessions.py revoke happy path — admin revokes ANOTHER user's session
    (not their own), returns the message, and the row is deleted."""
    _mk_user(admin_client, "Cov Revoke", "cov.revoke@example.com")
    other_c = _new_client()
    _login(other_c, "cov.revoke@example.com", "User12345")

    rows = admin_client.get("/api/sessions").json()
    victim = next(r for r in rows if r["user_email"] == "cov.revoke@example.com")
    r = admin_client.delete(f"/api/sessions/{victim['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "Session revoked"
    # Gone from the list now.
    after = {row["id"] for row in admin_client.get("/api/sessions").json()}
    assert victim["id"] not in after


def test_cov_revoke_own_session_blocked(admin_client):
    """sessions.py 117-121 — admin cannot revoke their OWN current session."""
    rows = admin_client.get("/api/sessions").json()
    mine = next(r for r in rows if r["is_current"])
    r = admin_client.delete(f"/api/sessions/{mine['id']}")
    assert r.status_code == 400
    assert "your own current session" in r.json()["detail"].lower()


def test_cov_revoke_session_404(admin_client):
    """sessions.py 112 — revoking a non-existent session id → 404."""
    r = admin_client.delete("/api/sessions/909090")
    assert r.status_code == 404
    assert r.json()["detail"] == "Session not found"


# ===========================================================================
# audit.py
# ===========================================================================
def test_cov_audit_filter_by_entity_actor_and_numeric_query(admin_client):
    """audit.py — exercises the filter/pagination branch including the numeric
    query path (89-103: entity_type filter, actor_user_id filter, digit match
    that appends the entity_id / bug_id / cast clauses, plus limit/offset).

    We search with q='42' style numerics and an entity_type so the OR-clause
    builder runs end-to-end. Asserts only that the endpoint returns 200 and a
    list — the exact rows depend on ids, which we don't pin."""
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
    # The numeric/entity filters should still surface this bug's creation row.
    assert any(row.get("bug_id") == bug["id"] or row.get("entity_id") == bug["id"]
               for row in rows), rows


def test_cov_audit_query_with_hash_and_offset(admin_client):
    """audit.py — second pass over the numeric branch using a '#'-prefixed
    query and a non-zero offset, plus the entity_id substring cast clause
    (101-102). Just needs to run green and return a list."""
    p = _mk_project(admin_client, "Cov Audit Proj 2")
    _mk_bug(admin_client, p["id"], title="Cov Audit Bug 2")
    r = admin_client.get("/api/audit", params={"q": "#1", "limit": 10, "offset": 1})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ===========================================================================
# notifications.py
# ===========================================================================
def test_cov_mark_read_already_read_skips_commit(admin_client):
    """notifications.py 75->78 — calling mark-read on an ALREADY-read
    notification skips the read_at/commit block and returns {'ok': True}.

    We generate a real notification (admin reports a bug assigned to a user),
    read it once (sets read_at), then read it again — the second call must hit
    the 75->78 branch (read_at is not None) and still return ok."""
    p = _mk_project(admin_client, "Cov Notif Proj")
    bob = _mk_user(admin_client, "Cov Notif Bob", "cov.notif.bob@example.com")
    _mk_bug(admin_client, p["id"], title="Cov Notif Bug", assignee_ids=[bob["id"]])

    bob_c = _new_client()
    _login(bob_c, "cov.notif.bob@example.com", "User12345")
    notifs = bob_c.get("/api/notifications").json()
    assert notifs, "Bob should have an 'assigned' notification"
    nid = notifs[0]["id"]

    # First read marks it read (the read_at-is-None TRUE branch).
    r1 = bob_c.post(f"/api/notifications/{nid}/read")
    assert r1.status_code == 200 and r1.json() == {"ok": True}

    # Second read: read_at is already set, so 75->78 short-circuits past commit.
    r2 = bob_c.post(f"/api/notifications/{nid}/read")
    assert r2.status_code == 200 and r2.json() == {"ok": True}

    # Idempotent: still exactly one row, still read.
    row = next(n for n in bob_c.get("/api/notifications").json() if n["id"] == nid)
    assert row["read_at"] is not None


# ===========================================================================
# Intentionally-uncovered lines (kept out on purpose — see RULE 5)
# ===========================================================================
# A few originally-listed target lines are DEFENSIVE / DEAD code that no
# clean, non-brittle API test can reach. Rather than ship a flaky or
# contorted test, they are left uncovered and documented here:
#
#   - sessions.py:44  (_is_current: `if not parsed: return False`)
#       Unreachable. Every endpoint that calls _is_current is admin-gated, and
#       auth (`_user_from_request`) authenticates ONLY via the same
#       `parse_session_token(cookie)`. So any request that reaches the handler
#       necessarily has a cookie that parses to a truthy tuple — the `not
#       parsed` guard can't fire. It's defensive-only against a malformed
#       cookie that would already have been rejected at auth.
#
#   - audit.py:96-97  (`except ValueError: pass` after `int(digits_match...)`)
#       Unreachable. `digits_match` comes from `re.search(r"\d+", raw)`, so the
#       captured group is pure ASCII digits, and Python's `int()` parses any
#       pure-digit string (arbitrary precision — no overflow). The `int(...)`
#       call therefore never raises ValueError, so the except body is dead.
#
#   - users.py:178  (last-admin guardrail raise on update)
#   - users.py:267  (last-admin guardrail raise on delete)
#       Unreachable raises. Both are pre-empted: when actor == target the
#       self-edit / self-delete guards (lines 161-164 / 258) fire first; when
#       actor != target the actor is themselves an active admin, so the
#       `n_other_admins` count is always >= 1 and the `== 0` branch is never
#       taken. The reachable parts of these checks (the count queries at
#       173-177 and 262-266) ARE exercised by
#       test_cov_update_user_demote_other_admin_runs_last_admin_check and
#       test_cov_delete_user_admin_runs_last_admin_count; only the dead raises
#       remain uncovered.
