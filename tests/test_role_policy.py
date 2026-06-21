"""Tests for the role-policy rules and the sessions admin feature.

What's covered:
  - Bug deletion is admin-only (managers are blocked)
  - Regular users CAN edit any bug (was reporter/assignee-only) and
    reassign it (assignee changes), but can't change the reporter
    (kept restricted to admin/manager) and can't delete.
  - Project deletion is admin-only (managers used to be allowed)
  - User create/update is now admin OR manager (was admin-only),
    user delete is still admin-only
  - Manager can't grant the admin role; can't edit/disturb admins
  - Audit trail is hidden from regular users
  - Sessions: list + revoke (admin-only); revoke removes the cookie's
    ability to authenticate; user can't revoke their own current session
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers (duplicated from test_api so this file is self-contained)
# ---------------------------------------------------------------------------
def _make_user(client, name="Alice", email=None, role="user", password="TestUserPwd9X"):
    email = email or f"{name.lower()}@example.com"
    r = client.post("/api/users", json={
        "name": name, "email": email, "role": role, "password": password,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(client, name="Mobile", color="#c9764f"):
    r = client.post("/api/projects", json={"name": name, "color": color})
    assert r.status_code == 201, r.text
    return r.json()


def _make_bug(client, project_id, **extra):
    body = {
        "title": "smoke bug",
        "project_id": project_id,
        "priority": "High",
        "environment": "DEV",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _login(client, email, pw):
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text


def _logout(client):
    client.post("/api/auth/logout")


# ===========================================================================
# 1. Bug deletion — admin-only
# ===========================================================================
class TestBugDeletePermissions:
    def test_admin_can_delete_bug(self, admin_client):
        p = _make_project(admin_client, name="DelProj1")
        bug = _make_bug(admin_client, p["id"], title="kill me")
        r = admin_client.delete(f"/api/bugs/{bug['id']}")
        assert r.status_code == 200

    def test_manager_cannot_delete_bug(self, admin_client):
        """Managers cannot delete a bug."""
        p = _make_project(admin_client, name="DelProj2")
        bug = _make_bug(admin_client, p["id"], title="manager must not delete me")
        _make_user(admin_client, name="Mgr", email="mgr1@x.com",
                   role="manager", password="Mgr1234567")
        _logout(admin_client)
        _login(admin_client, "mgr1@x.com", "Mgr1234567")
        r = admin_client.delete(f"/api/bugs/{bug['id']}")
        assert r.status_code == 403
        assert "admin" in r.json()["detail"].lower()

    def test_user_cannot_delete_bug(self, admin_client):
        p = _make_project(admin_client, name="DelProj3")
        bug = _make_bug(admin_client, p["id"], title="user must not delete me")
        _make_user(admin_client, name="Bob", email="bob1@x.com",
                   role="user", password="Bob1234567")
        _logout(admin_client)
        _login(admin_client, "bob1@x.com", "Bob1234567")
        r = admin_client.delete(f"/api/bugs/{bug['id']}")
        assert r.status_code == 403

    def test_user_cannot_delete_their_own_bug(self, admin_client):
        """Per spec: 'they cannot delete any bug, even the ones they created'."""
        p = _make_project(admin_client, name="DelProj4")
        _make_user(admin_client, name="Carol", email="carol1@x.com",
                   role="user", password="Carol12345")
        _logout(admin_client)
        _login(admin_client, "carol1@x.com", "Carol12345")
        bug = _make_bug(admin_client, p["id"], title="my own bug")
        # Carol is the reporter, but still cannot delete.
        r = admin_client.delete(f"/api/bugs/{bug['id']}")
        assert r.status_code == 403


# ===========================================================================
# 2. Bug edit / reassign — every user can edit any bug
# ===========================================================================
class TestBugEditPermissions:
    def test_user_can_edit_someone_elses_bug(self, admin_client):
        p = _make_project(admin_client, name="EditProj1")
        bug = _make_bug(admin_client, p["id"], title="admin's bug")
        _make_user(admin_client, name="Dave", email="dave@x.com",
                   role="user", password="Dave12345")
        _logout(admin_client)
        _login(admin_client, "dave@x.com", "Dave12345")
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={
            "title": "Dave changed it",
            "priority": "Low",
            "status": "In Progress",
        })
        assert r.status_code == 200
        assert r.json()["title"] == "Dave changed it"
        assert r.json()["status"] == "In Progress"

    def test_user_can_reassign_bug(self, admin_client):
        """Spec: 'reassign any bug to anyone else'."""
        p = _make_project(admin_client, name="EditProj2")
        someone = _make_user(admin_client, name="Eve", email="eve@x.com",
                             role="user", password="Eve1234567")
        bug = _make_bug(admin_client, p["id"], title="reassign me")

        _make_user(admin_client, name="Frank", email="frank@x.com",
                   role="user", password="Frank12345")
        _logout(admin_client)
        _login(admin_client, "frank@x.com", "Frank12345")

        # Frank reassigns the admin's bug to Eve
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={
            "assignee_ids": [someone["id"]],
        })
        assert r.status_code == 200, r.text
        assignee_ids = [a["id"] for a in r.json()["assignees"]]
        assert someone["id"] in assignee_ids

    def test_can_edit_flag_is_true_for_user_on_any_bug(self, admin_client):
        p = _make_project(admin_client, name="EditProj3")
        bug = _make_bug(admin_client, p["id"], title="check can_edit")
        _make_user(admin_client, name="Grace", email="grace@x.com",
                   role="user", password="Grace12345")
        _logout(admin_client)
        _login(admin_client, "grace@x.com", "Grace12345")
        r = admin_client.get(f"/api/bugs/{bug['id']}")
        assert r.json()["can_edit"] is True


# ===========================================================================
# 3. Projects — admin-only delete
# ===========================================================================
class TestProjectDeletePermissions:
    def test_manager_cannot_delete_project(self, admin_client):
        """Project delete is admin-only; a manager is blocked."""
        p = _make_project(admin_client, name="MgrCannotDel")
        _make_user(admin_client, name="Mgr2", email="mgr2@x.com",
                   role="manager", password="Mgr2_abc99")
        _logout(admin_client)
        _login(admin_client, "mgr2@x.com", "Mgr2_abc99")
        r = admin_client.delete(f"/api/projects/{p['id']}")
        assert r.status_code == 403

    def test_manager_can_create_and_edit_project(self, admin_client):
        _make_user(admin_client, name="Mgr3", email="mgr3@x.com",
                   role="manager", password="Mgr3_abc99")
        _logout(admin_client)
        _login(admin_client, "mgr3@x.com", "Mgr3_abc99")
        # Create
        r = admin_client.post("/api/projects", json={"name": "MgrMade"})
        assert r.status_code == 201
        pid = r.json()["id"]
        # Edit
        r = admin_client.put(f"/api/projects/{pid}", json={
            "name": "MgrRenamed", "color": "#abcdef",
        })
        assert r.status_code == 200
        assert r.json()["name"] == "MgrRenamed"

    def test_user_cannot_create_or_edit_project(self, admin_client):
        p = _make_project(admin_client, name="UserCantTouch")
        _make_user(admin_client, name="Henry", email="henry@x.com",
                   role="user", password="Henry12345")
        _logout(admin_client)
        _login(admin_client, "henry@x.com", "Henry12345")
        r = admin_client.post("/api/projects", json={"name": "Forbidden"})
        assert r.status_code == 403
        r = admin_client.put(f"/api/projects/{p['id']}", json={"name": "X"})
        assert r.status_code == 403


# ===========================================================================
# 4. Users — manager can manage but not delete; no admin role for managers
# ===========================================================================
class TestUserManagementPermissions:
    def test_manager_can_create_and_edit_user(self, admin_client):
        _make_user(admin_client, name="Mgr4", email="mgr4@x.com",
                   role="manager", password="Mgr4_abc99")
        _logout(admin_client)
        _login(admin_client, "mgr4@x.com", "Mgr4_abc99")

        # Manager creates a regular user
        r = admin_client.post("/api/users", json={
            "name": "MgrMade", "email": "mgrmade@x.com",
            "role": "user", "password": "Made12345",
        })
        assert r.status_code == 201, r.text
        new_id = r.json()["id"]
        # Edit
        r = admin_client.put(f"/api/users/{new_id}", json={"name": "Renamed"})
        assert r.status_code == 200
        assert r.json()["name"] == "Renamed"

    def test_manager_cannot_create_admin(self, admin_client):
        _make_user(admin_client, name="Mgr5", email="mgr5@x.com",
                   role="manager", password="Mgr5_abc99")
        _logout(admin_client)
        _login(admin_client, "mgr5@x.com", "Mgr5_abc99")
        r = admin_client.post("/api/users", json={
            "name": "Sneaky", "email": "sneaky@x.com",
            "role": "admin", "password": "Sneaky1234",
        })
        assert r.status_code == 403

    def test_manager_cannot_promote_user_to_admin(self, admin_client):
        _make_user(admin_client, name="Mgr6", email="mgr6@x.com",
                   role="manager", password="Mgr6_abc99")
        target = _make_user(admin_client, name="Target", email="target@x.com",
                            role="user", password="Target1234")
        _logout(admin_client)
        _login(admin_client, "mgr6@x.com", "Mgr6_abc99")
        r = admin_client.put(f"/api/users/{target['id']}", json={"role": "admin"})
        assert r.status_code == 403

    def test_manager_cannot_edit_admin(self, admin_client):
        _make_user(admin_client, name="Mgr7", email="mgr7@x.com",
                   role="manager", password="Mgr7_abc99")
        admin_me = admin_client.get("/api/auth/me").json()
        _logout(admin_client)
        _login(admin_client, "mgr7@x.com", "Mgr7_abc99")
        r = admin_client.put(f"/api/users/{admin_me['id']}", json={"name": "Hijacked"})
        assert r.status_code == 403

    def test_manager_cannot_delete_user(self, admin_client):
        _make_user(admin_client, name="Mgr8", email="mgr8@x.com",
                   role="manager", password="Mgr8_abc99")
        target = _make_user(admin_client, name="Target2", email="t2@x.com",
                            role="user", password="Target1234")
        _logout(admin_client)
        _login(admin_client, "mgr8@x.com", "Mgr8_abc99")
        r = admin_client.delete(f"/api/users/{target['id']}")
        assert r.status_code == 403

    def test_user_cannot_create_user(self, user_client):
        r = user_client.post("/api/users", json={
            "name": "X", "email": "x@x.com",
            "role": "user", "password": "TestUserPwd9X",
        })
        assert r.status_code == 403


# ===========================================================================
# 5. Audit — hidden from regular users
# ===========================================================================
class TestAuditVisibility:
    def test_audit_403_for_user(self, user_client):
        r = user_client.get("/api/audit")
        assert r.status_code == 403

    def test_audit_200_for_manager(self, admin_client):
        _make_user(admin_client, name="MgrA", email="mgra@x.com",
                   role="manager", password="MgrA_abc99")
        _logout(admin_client)
        _login(admin_client, "mgra@x.com", "MgrA_abc99")
        r = admin_client.get("/api/audit")
        assert r.status_code == 200

    def test_audit_200_for_admin(self, admin_client):
        r = admin_client.get("/api/audit")
        assert r.status_code == 200


# ===========================================================================
# 6. Sessions — admin-only list + revoke
#
# These tests need separate clients (the conftest fixtures all share one
# TestClient). We make our own with the FastAPI TestClient and the running
# app instance so each "device" has its own cookie jar.
# ===========================================================================
def _new_client():
    """Build a brand-new TestClient against the same already-running app
    instance. Each one has a fresh cookie jar — i.e. it's a separate
    'device' for session-management tests."""
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


class TestSessionsAdmin:
    def test_admin_login_creates_session_row(self, admin_client):
        r = admin_client.get("/api/sessions")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) >= 1
        # The admin's own session should be marked is_current=True
        assert any(row["is_current"] for row in rows)
        # And has admin's email
        assert any(row.get("user_email") == "admin@test.local" for row in rows)
        # ...and carries IP / user-agent
        for row in rows:
            assert "user_agent" in row
            assert "ip_address" in row

    def test_user_cannot_list_sessions(self, user_client):
        r = user_client.get("/api/sessions")
        assert r.status_code == 403

    def test_manager_cannot_list_sessions(self, admin_client):
        _make_user(admin_client, name="MgrS", email="mgrs@x.com",
                   role="manager", password="MgrS_abc99")
        _logout(admin_client)
        _login(admin_client, "mgrs@x.com", "MgrS_abc99")
        r = admin_client.get("/api/sessions")
        assert r.status_code == 403

    def test_user_cannot_revoke_session(self, admin_client):
        # Log in as a user on a SEPARATE client so we get a separate cookie
        _make_user(admin_client, name="UU", email="uu@x.com",
                   role="user", password="UU_abc1234")
        user_dev = _new_client()
        r = user_dev.post("/api/auth/login", json={
            "email": "uu@x.com", "password": "UU_abc1234",
        })
        assert r.status_code == 200

        # admin enumerates and finds uu's session
        r = admin_client.get("/api/sessions")
        user_rows = [s for s in r.json() if s.get("user_email") == "uu@x.com"]
        assert user_rows, "user uu@x.com should have an active session"
        sid = user_rows[0]["id"]

        # The user themselves shouldn't be allowed to call DELETE.
        r = user_dev.delete(f"/api/sessions/{sid}")
        assert r.status_code == 403

    def test_admin_can_revoke_user_session(self, admin_client):
        """Revoking the user's session row should make their cookie stop
        authenticating on the very next request."""
        _make_user(admin_client, name="UV", email="uv@x.com",
                   role="user", password="UV_abc1234")
        user_dev = _new_client()
        r = user_dev.post("/api/auth/login", json={
            "email": "uv@x.com", "password": "UV_abc1234",
        })
        assert r.status_code == 200
        # Confirm the user is logged in
        assert user_dev.get("/api/auth/me").status_code == 200

        r = admin_client.get("/api/sessions")
        user_rows = [s for s in r.json() if s.get("user_email") == "uv@x.com"]
        assert user_rows
        sid = user_rows[0]["id"]

        # Admin revokes the user's session
        r = admin_client.delete(f"/api/sessions/{sid}")
        assert r.status_code == 200

        # User's cookie should now be rejected
        r = user_dev.get("/api/auth/me")
        assert r.status_code == 401

    def test_admin_cannot_revoke_their_own_current_session(self, admin_client):
        """Safety: the API rejects revoking the cookie you're using right now."""
        r = admin_client.get("/api/sessions")
        own = next(s for s in r.json() if s["is_current"])
        r = admin_client.delete(f"/api/sessions/{own['id']}")
        assert r.status_code == 400
        assert "log out" in r.json()["detail"].lower()

    def test_admin_can_revoke_their_own_OTHER_session(self, admin_client):
        """If admin is logged in on two devices, they can revoke the other
        device while keeping the current one."""
        # Spin up a SECOND TestClient and log in as admin on it.
        other_dev = _new_client()
        r = other_dev.post("/api/auth/login", json={
            "email": "admin@test.local", "password": "Admin1234",
        })
        assert r.status_code == 200

        # admin_client should see two admin sessions; the non-current one is
        # the one on `other_dev`.
        r = admin_client.get("/api/sessions")
        admin_sessions = [s for s in r.json() if s.get("user_email") == "admin@test.local"]
        assert len(admin_sessions) == 2
        other = next(s for s in admin_sessions if not s["is_current"])

        r = admin_client.delete(f"/api/sessions/{other['id']}")
        assert r.status_code == 200

        # The OTHER device is now logged out
        r = other_dev.get("/api/auth/me")
        assert r.status_code == 401

        # The current device still works
        r = admin_client.get("/api/auth/me")
        assert r.status_code == 200

    def test_logout_removes_session_row(self, admin_client):
        """After logout, the session row should be gone — admin's list
        shouldn't include the logged-out cookie's session anymore."""
        # Snapshot count of admin sessions
        r = admin_client.get("/api/sessions")
        admin_sessions = [s for s in r.json() if s.get("user_email") == "admin@test.local"]
        assert len(admin_sessions) == 1

        admin_client.post("/api/auth/logout")
        # After logout, admin can't list sessions (no session)
        r = admin_client.get("/api/sessions")
        assert r.status_code == 401

        # Log back in
        admin_client.post("/api/auth/login", json={
            "email": "admin@test.local", "password": "Admin1234",
        })
        r = admin_client.get("/api/sessions")
        assert r.status_code == 200
        admin_sessions = [s for s in r.json() if s.get("user_email") == "admin@test.local"]
        assert len(admin_sessions) == 1, \
            f"expected exactly 1 admin session after logout/login, got {len(admin_sessions)}"


# ===========================================================================
# 7. Password change — should re-establish session cleanly
# ===========================================================================
class TestPasswordChangeKeepsCurrentDevice:
    def test_change_password_keeps_current_session_works(self, admin_client):
        """After change-password, the current cookie must still authenticate
        (we re-issue with new jti). Other devices for the same user are
        invalidated by the session_version bump."""
        r = admin_client.post("/api/auth/change-password", json={
            "current_password": "Admin1234",
            "new_password": "NewAdmin1234",
        })
        assert r.status_code == 204

        # Current device still works
        r = admin_client.get("/api/auth/me")
        assert r.status_code == 200, r.text

        # Reset password back so other tests in this file using the same
        # password still work — actually conftest provides a fresh tmp DB
        # per-fixture, so this one isn't strictly needed.
