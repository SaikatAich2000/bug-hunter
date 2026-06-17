"""Tests for per-item-type status sets and admin-only comment/attachment edits.

  1. Per-item-type status sets — "Not a Bug" / "Resolved" only on Bug;
     "Approved" / "Implemented" only on Requirement; "Done" / "Blocked"
     / "Cancelled" only on Task; "New" shared by every type.
  2. Comments — edit + delete admin-only (creation still open).
  3. Attachments — delete admin-only (upload still open to anyone
     who can edit the bug, including post-creation).
  4. /api/meta exposes statuses_by_type for the frontend.

These behaviors are enforced server-side (the SPA mirrors them but the
authoritative check is here).
"""
from __future__ import annotations

import io


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _login(client, email, password):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _make_user(client, name, role="user", email=None, password="User12345Aa"):
    email = email or f"{name.lower()}@x.test"
    r = client.post("/api/users", json={
        "name": name, "email": email, "role": role, "password": password,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(client, name="Eng"):
    r = client.post("/api/projects", json={"name": name, "color": "#c9764f"})
    assert r.status_code == 201, r.text
    return r.json()


def _make_item(client, project_id, item_type="Bug", **extra):
    body = {
        "title": f"Sample {item_type}",
        "project_id": project_id,
        "item_type": item_type,
        "priority": "Medium",
        "environment": "DEV",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. Per-item-type status sets
# ---------------------------------------------------------------------------
def test_status_new_is_shared_by_every_type(admin_client):
    p = _make_project(admin_client)
    for itype in ("Bug", "Requirement", "Task"):
        body = {
            "title": f"{itype} test",
            "project_id": p["id"],
            "item_type": itype,
            "priority": "Medium",
            "environment": "DEV",
            "status": "New",
        }
        r = admin_client.post("/api/bugs", json=body)
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "New"


def test_task_cannot_be_created_with_not_a_bug_status(admin_client):
    p = _make_project(admin_client)
    r = admin_client.post("/api/bugs", json={
        "title": "Bad task", "project_id": p["id"], "item_type": "Task",
        "priority": "Medium", "environment": "DEV", "status": "Not a Bug",
    })
    assert r.status_code == 422, r.text
    # Pydantic error mentions the disallowed status + the valid set.
    body = r.json()
    msgs = [d.get("msg", "") for d in body.get("detail", [])]
    joined = " ".join(msgs).lower()
    assert "task" in joined
    assert "not a bug" in joined


def test_requirement_cannot_move_to_resolved(admin_client):
    p = _make_project(admin_client)
    req = _make_item(admin_client, p["id"], item_type="Requirement")
    r = admin_client.put(f"/api/bugs/{req['id']}", json={"status": "Resolved"})
    assert r.status_code == 400, r.text
    assert "requirement" in r.json()["detail"].lower()


def test_bug_can_use_not_a_bug_status(admin_client):
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "Not a Bug"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Not a Bug"


def test_task_can_use_done_status(admin_client):
    p = _make_project(admin_client)
    task = _make_item(admin_client, p["id"], item_type="Task")
    r = admin_client.put(f"/api/bugs/{task['id']}", json={"status": "Done"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Done"


def test_requirement_can_use_approved_status(admin_client):
    p = _make_project(admin_client)
    req = _make_item(admin_client, p["id"], item_type="Requirement")
    r = admin_client.put(f"/api/bugs/{req['id']}", json={"status": "Approved"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Approved"


def test_changing_type_validates_status_against_new_type(admin_client):
    """If a bug is currently 'Not a Bug' and a single PUT tries to flip it
    to a Task, the request must fail — Task can't carry that status.
    (Flipping the type WITHOUT changing the status is a quiet bug-data
    scenario the server tolerates; this asserts the explicit reject
    path.)"""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "Not a Bug"})
    assert r.status_code == 200
    # Flip the type to Task while resetting status to something invalid
    # for Task → reject.
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={
        "item_type": "Task", "status": "Resolved",
    })
    assert r.status_code == 400


def test_meta_endpoint_exposes_statuses_by_type(admin_client):
    r = admin_client.get("/api/meta")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "statuses_by_type" in body
    sbt = body["statuses_by_type"]
    assert "Bug" in sbt and "Requirement" in sbt and "Task" in sbt
    # Spec invariants.
    assert "New" in sbt["Bug"]
    assert "New" in sbt["Requirement"]
    assert "New" in sbt["Task"]
    assert "Not a Bug" in sbt["Bug"]
    assert "Not a Bug" not in sbt["Task"]
    assert "Not a Bug" not in sbt["Requirement"]
    assert "Done" in sbt["Task"]
    assert "Done" not in sbt["Bug"]
    assert "Approved" in sbt["Requirement"]
    assert "Approved" not in sbt["Task"]


# ---------------------------------------------------------------------------
# 2. Comments — admin-only edit + delete
# ---------------------------------------------------------------------------
def test_comment_delete_is_admin_only(admin_client):
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    # Manager creates a comment.
    _make_user(admin_client, "Mgr", role="manager")
    _login(admin_client, "mgr@x.test", "User12345Aa")
    r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "from manager"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    # Manager tries to delete their own comment → 403.
    r = admin_client.delete(f"/api/bugs/{bug['id']}/comments/{cid}")
    assert r.status_code == 403
    assert "admin" in r.json()["detail"].lower()
    # Admin can delete.
    _login(admin_client, "admin@test.local", "Admin1234")
    r = admin_client.delete(f"/api/bugs/{bug['id']}/comments/{cid}")
    assert r.status_code == 200


def test_comment_edit_is_admin_only(admin_client):
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    # Regular user files a comment.
    _make_user(admin_client, "User1", role="user", email="user1@x.test")
    _login(admin_client, "user1@x.test", "User12345Aa")
    r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "original body"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    # The author tries to edit → 403.
    r = admin_client.put(f"/api/bugs/{bug['id']}/comments/{cid}", json={"body": "edited"})
    assert r.status_code == 403
    assert "admin" in r.json()["detail"].lower()
    # Admin can edit and the body changes.
    _login(admin_client, "admin@test.local", "Admin1234")
    r = admin_client.put(f"/api/bugs/{bug['id']}/comments/{cid}", json={"body": "edited by admin"})
    assert r.status_code == 200
    assert r.json()["body"] == "edited by admin"


def test_comment_creation_still_open_for_everyone(admin_client):
    """Only edit + delete are restricted — creating comments must still
    work for anyone authenticated, otherwise users can't add evidence."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    _make_user(admin_client, "User2", role="user", email="user2@x.test")
    _login(admin_client, "user2@x.test", "User12345Aa")
    r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "evidence"})
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# 3. Attachments — admin-only delete, upload still open
# ---------------------------------------------------------------------------
def test_attachment_delete_is_admin_only(admin_client):
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    files = {"file": ("a.txt", io.BytesIO(b"contents"), "text/plain")}
    r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
    assert r.status_code == 201
    att_id = r.json()["id"]
    # Manager can't delete (used to be allowed).
    _make_user(admin_client, "Mgr2", role="manager", email="mgr2@x.test")
    _login(admin_client, "mgr2@x.test", "User12345Aa")
    r = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
    assert r.status_code == 403
    assert "admin" in r.json()["detail"].lower()
    # Admin can.
    _login(admin_client, "admin@test.local", "Admin1234")
    r = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
    assert r.status_code == 200


def test_attachment_upload_open_post_creation(admin_client):
    """Users can still attach evidence after a bug is filed — the
    restriction only applies to delete."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    _make_user(admin_client, "User3", role="user", email="user3@x.test")
    _login(admin_client, "user3@x.test", "User12345Aa")
    # Bug-level upload after the bug exists.
    files = {"file": ("evidence.txt", io.BytesIO(b"after creation"), "text/plain")}
    r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
    assert r.status_code == 201
    assert r.json()["uploader_name"] == "User3"


def test_comment_attachment_delete_is_admin_only(admin_client):
    """Attachments on a comment also drop into the admin-only delete
    rule (the route is the same DELETE /attachments/{id} endpoint)."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    # User adds a comment with an attachment.
    _make_user(admin_client, "User4", role="user", email="user4@x.test")
    _login(admin_client, "user4@x.test", "User12345Aa")
    r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "see file"})
    cid = r.json()["id"]
    files = {"file": ("log.txt", io.BytesIO(b"stuff"), "text/plain")}
    r = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files=files,
        data={"comment_id": str(cid)},
    )
    assert r.status_code == 201
    att_id = r.json()["id"]
    # Author CAN'T delete their own attachment.
    r = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
    assert r.status_code == 403
    # Admin can.
    _login(admin_client, "admin@test.local", "Admin1234")
    r = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
    assert r.status_code == 200


def test_audit_log_records_admin_comment_actions(admin_client):
    """Admin edit + delete of comments should be auditable."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "initial"})
    cid = r.json()["id"]
    r = admin_client.put(f"/api/bugs/{bug['id']}/comments/{cid}", json={"body": "edited"})
    assert r.status_code == 200
    r = admin_client.delete(f"/api/bugs/{bug['id']}/comments/{cid}")
    assert r.status_code == 200
    # Audit feed should contain both rows.
    r = admin_client.get("/api/audit?entity_type=comment&limit=50")
    assert r.status_code == 200
    actions = {row["action"] for row in r.json()}
    assert "comment_edited" in actions
    assert "comment_deleted" in actions
