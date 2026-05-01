"""End-to-end API tests for Bug Hunter v3 (with auth)."""
from __future__ import annotations

import io


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_user(client, name="Alice", email=None, role="user", password="Password1"):
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


# ---------------------------------------------------------------------------
# Public meta endpoints (no auth)
# ---------------------------------------------------------------------------
def test_health_and_meta_no_auth(client):
    """/api/health and /api/meta deliberately don't require auth."""
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    body = client.get("/api/meta").json()
    assert "New" in body["statuses"]
    assert "Critical" in body["priorities"]
    assert body["environments"] == ["DEV", "UAT", "PROD"]


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
def test_unauthenticated_endpoints_return_401(client):
    for path in ("/api/bugs", "/api/projects", "/api/users", "/api/stats", "/api/audit"):
        r = client.get(path)
        assert r.status_code == 401, f"{path} returned {r.status_code}, expected 401"


def test_login_with_wrong_password(client):
    r = client.post("/api/auth/login", json={
        "email": "admin@test.local", "password": "wrong",
    })
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"].lower()


def test_login_logout_me(client):
    # Login
    r = client.post("/api/auth/login", json={
        "email": "admin@test.local", "password": "Admin1234",
    })
    assert r.status_code == 200
    me = r.json()
    assert me["email"] == "admin@test.local"
    assert me["role"] == "admin"

    # /me works
    r = client.get("/api/auth/me")
    assert r.status_code == 200

    # Logout
    r = client.post("/api/auth/logout")
    assert r.status_code == 204

    # /me now 401
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_admin_can_change_password(admin_client):
    r = admin_client.post("/api/auth/change-password", json={
        "current_password": "Admin1234",
        "new_password": "NewerPass456",
    })
    assert r.status_code == 204

    # Old password fails
    admin_client.post("/api/auth/logout")
    r = admin_client.post("/api/auth/login", json={
        "email": "admin@test.local", "password": "Admin1234",
    })
    assert r.status_code == 401

    # New one works
    r = admin_client.post("/api/auth/login", json={
        "email": "admin@test.local", "password": "NewerPass456",
    })
    assert r.status_code == 200


def test_change_password_requires_correct_current(admin_client):
    r = admin_client.post("/api/auth/change-password", json={
        "current_password": "WrongCurrent",
        "new_password": "NewerPass456",
    })
    assert r.status_code == 400


def test_forgot_password_no_enumeration(client):
    """Both real and fake emails must return 204 — no info leakage."""
    r = client.post("/api/auth/forgot-password", json={"email": "admin@test.local"})
    assert r.status_code == 204
    r = client.post("/api/auth/forgot-password", json={"email": "doesnotexist@nowhere.test"})
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# User CRUD (admin only)
# ---------------------------------------------------------------------------
def test_user_crud_admin(admin_client):
    u = _make_user(admin_client, name="Alice", email="alice@example.com",
                   role="user", password="Alice1234")
    assert u["role"] == "user"
    assert "password_hash" not in u  # never serialized

    r = admin_client.put(f"/api/users/{u['id']}", json={"role": "manager"})
    assert r.status_code == 200 and r.json()["role"] == "manager"

    r = admin_client.delete(f"/api/users/{u['id']}")
    assert r.status_code == 200


def test_regular_user_cannot_create_users(user_client):
    r = user_client.post("/api/users", json={
        "name": "Bob", "email": "bob@example.com",
        "role": "user", "password": "Bob1234567",
    })
    assert r.status_code == 403


def test_admin_cannot_delete_self(admin_client):
    me = admin_client.get("/api/auth/me").json()
    r = admin_client.delete(f"/api/users/{me['id']}")
    assert r.status_code == 400
    assert "yourself" in r.json()["detail"].lower()


def test_admin_cannot_demote_self_to_user(admin_client):
    me = admin_client.get("/api/auth/me").json()
    r = admin_client.put(f"/api/users/{me['id']}", json={"role": "user"})
    assert r.status_code == 400


def test_cannot_remove_last_admin(admin_client):
    """Even if a different admin tries to demote/delete the last admin, it's blocked."""
    # Right now there's only one admin (the bootstrap). Create another one,
    # then try to demote the bootstrap admin.
    other = _make_user(admin_client, name="Other", email="other@example.com",
                       role="admin", password="Other1234")
    # Now demoting any single admin is OK because there are 2.
    r = admin_client.put(f"/api/users/{other['id']}", json={"role": "user"})
    assert r.status_code == 200
    # But demoting the now-only-admin fails
    me = admin_client.get("/api/auth/me").json()
    r = admin_client.put(f"/api/users/{me['id']}", json={"role": "user"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Projects (manager+ only for write)
# ---------------------------------------------------------------------------
def test_project_create_admin(admin_client):
    p = _make_project(admin_client, name="Mobile App")
    assert p["name"] == "Mobile App"


def test_regular_user_cannot_create_project(user_client):
    r = user_client.post("/api/projects", json={"name": "Forbidden", "color": "#000"})
    assert r.status_code == 403


def test_regular_user_can_list_projects(user_client):
    r = user_client.get("/api/projects")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Bugs + can_edit + permissions
# ---------------------------------------------------------------------------
def test_admin_creates_bug_can_edit_true(admin_client):
    p = _make_project(admin_client, name="P1")
    bug = _make_bug(admin_client, p["id"], title="Test bug")
    assert bug["can_edit"] is True
    assert bug["reporter"]["email"] == "admin@test.local"


def test_user_creates_bug_can_edit_true(user_client):
    # Set up: admin needs to make a project first
    me = user_client.get("/api/auth/me").json()
    # Log out user, log in as admin to create a project
    user_client.post("/api/auth/logout")
    user_client.post("/api/auth/login", json={
        "email": "admin@test.local", "password": "Admin1234",
    })
    p = _make_project(user_client, name="UserProject")
    # Switch back to user
    user_client.post("/api/auth/logout")
    user_client.post("/api/auth/login", json={
        "email": "user@test.local", "password": "User12345",
    })

    bug = _make_bug(user_client, p["id"], title="My bug")
    assert bug["can_edit"] is True


def test_user_cannot_edit_others_bugs(admin_client):
    """Admin creates a bug; a regular user should see can_edit=False and 403 on edit."""
    p = _make_project(admin_client, name="ProjA")
    bug = _make_bug(admin_client, p["id"], title="Admin's bug")
    bug_id = bug["id"]

    # Create a regular user
    _make_user(admin_client, name="Bob", email="bob@example.com",
               role="user", password="Bob1234567")
    # Log out, log in as Bob
    admin_client.post("/api/auth/logout")
    admin_client.post("/api/auth/login", json={
        "email": "bob@example.com", "password": "Bob1234567",
    })

    # Bob sees can_edit=False on the admin's bug
    r = admin_client.get(f"/api/bugs/{bug_id}")
    assert r.json()["can_edit"] is False

    # Bob cannot update it
    r = admin_client.put(f"/api/bugs/{bug_id}", json={"title": "hacked"})
    assert r.status_code == 403

    # Bob cannot delete it
    r = admin_client.delete(f"/api/bugs/{bug_id}")
    assert r.status_code == 403


def test_user_can_edit_own_bug(admin_client):
    p = _make_project(admin_client, name="ProjB")
    _make_user(admin_client, name="Carol", email="carol@example.com",
               role="user", password="Carol1234")
    admin_client.post("/api/auth/logout")
    admin_client.post("/api/auth/login", json={
        "email": "carol@example.com", "password": "Carol1234",
    })

    bug = _make_bug(admin_client, p["id"], title="Carol's bug")
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "In Progress"})
    assert r.status_code == 200
    assert r.json()["status"] == "In Progress"


def test_manager_can_edit_anyones_bug(admin_client):
    p = _make_project(admin_client, name="ProjC")
    bug = _make_bug(admin_client, p["id"], title="Admin's bug")
    _make_user(admin_client, name="Mgr", email="mgr@example.com",
               role="manager", password="Mgr1234567")
    admin_client.post("/api/auth/logout")
    admin_client.post("/api/auth/login", json={
        "email": "mgr@example.com", "password": "Mgr1234567",
    })

    # Manager can edit
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"priority": "Critical"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Comments + attachments use session
# ---------------------------------------------------------------------------
def test_comment_uses_session_user(admin_client):
    p = _make_project(admin_client, name="ProjD")
    bug = _make_bug(admin_client, p["id"], title="With comment")
    r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "Hello"})
    assert r.status_code == 201
    assert r.json()["author_name"] == "Test Admin"


def test_attachment_upload_uses_session_user(admin_client):
    p = _make_project(admin_client, name="ProjE")
    bug = _make_bug(admin_client, p["id"], title="With attach")
    files = {"file": ("hello.txt", io.BytesIO(b"hello world"), "text/plain")}
    r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
    assert r.status_code == 201
    assert r.json()["uploader_name"] == "Test Admin"


# ---------------------------------------------------------------------------
# Stats + audit
# ---------------------------------------------------------------------------
def test_stats(admin_client):
    r = admin_client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert "bugs" in body
    assert "users" in body or "by_assignee" in body


def test_audit_records_login(admin_client):
    """Logging in should appear in the audit log."""
    r = admin_client.get("/api/audit")
    assert r.status_code == 200
    rows = r.json()
    assert any(row["action"] == "login" for row in rows), \
        "expected a 'login' entry in audit log"
