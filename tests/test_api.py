"""End-to-end API tests for Bug Hunter v3."""
from __future__ import annotations

import io


def _make_user(client, name="Alice", email=None, role="QA"):
    email = email or f"{name.lower()}@example.com"
    r = client.post("/api/users", json={"name": name, "email": email, "role": role})
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(client, name="Mobile", color="#c9764f"):
    r = client.post("/api/projects", json={"name": name, "color": color})
    assert r.status_code == 201, r.text
    return r.json()


def _make_bug(client, project_id, reporter_id, **extra):
    body = {
        "title": "smoke bug",
        "project_id": project_id,
        "reporter_id": reporter_id,
        "priority": "High",
        "environment": "DEV",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Meta + seeds
# ---------------------------------------------------------------------------
def test_health_and_meta(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    body = client.get("/api/meta").json()
    assert "New" in body["statuses"]
    assert "Critical" in body["priorities"]
    assert body["environments"] == ["DEV", "UAT", "PROD"]


def test_default_seeds(client):
    projects = client.get("/api/projects").json()
    assert any(p["name"] == "General" for p in projects)
    users = client.get("/api/users").json()
    assert any(u["email"] == "system@example.com" for u in users)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def test_user_crud_with_audit(client):
    u = _make_user(client, name="Alice", email="alice@example.com", role="Frontend Engineer")
    r = client.put(f"/api/users/{u['id']}", json={"role": "Senior FE"})
    assert r.status_code == 200 and r.json()["role"] == "Senior FE"
    r = client.delete(f"/api/users/{u['id']}")
    assert r.status_code == 200

    # Audit log should have user_created + user_updated + user_deleted
    audit = client.get("/api/audit?entity_type=user").json()
    actions = [a["action"] for a in audit]
    assert "user_created" in actions
    assert "user_updated" in actions
    assert "user_deleted" in actions


def test_user_email_unique(client):
    _make_user(client, email="bob@example.com")
    r = client.post("/api/users", json={"name": "Bob2", "email": "BOB@example.com", "role": ""})
    assert r.status_code == 409


def test_user_email_validation(client):
    r = client.post("/api/users", json={"name": "Bad", "email": "not-an-email", "role": ""})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
def test_project_crud_with_audit(client):
    p = _make_project(client, name="Web App")
    r = client.put(f"/api/projects/{p['id']}",
                   json={"name": "Web", "color": "#22c55e", "description": ""})
    assert r.status_code == 200
    r = client.delete(f"/api/projects/{p['id']}")
    assert r.status_code == 200

    audit = client.get("/api/audit?entity_type=project").json()
    assert any(a["action"] == "project_created" for a in audit)
    assert any(a["action"] == "project_deleted" for a in audit)


def test_project_delete_blocked_with_bugs(client):
    u = _make_user(client, email="reporter@example.com")
    p = _make_project(client, name="P1")
    _make_bug(client, p["id"], u["id"])
    r = client.delete(f"/api/projects/{p['id']}")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Bugs (note: NO severity, labels, steps_to_reproduce, etc. — should reject)
# ---------------------------------------------------------------------------
def test_bug_create_with_environment(client):
    rep = _make_user(client, email="rep@example.com")
    a1 = _make_user(client, name="Alice", email="alice@example.com")
    a2 = _make_user(client, name="Bob", email="bob@example.com")
    p = _make_project(client, name="Web")

    bug = _make_bug(
        client, p["id"], rep["id"],
        title="Login broken on iOS", environment="prod",
        assignee_ids=[a1["id"], a2["id"]],
    )
    assert bug["environment"] == "PROD"
    assert {a["email"] for a in bug["assignees"]} == {"alice@example.com", "bob@example.com"}


def test_bug_environment_validation(client):
    rep = _make_user(client, email="r@example.com")
    p = _make_project(client, name="ProjA")
    r = client.post("/api/bugs", json={
        "title": "Test", "project_id": p["id"],
        "reporter_id": rep["id"], "environment": "INVALID",
    })
    assert r.status_code == 422


def test_bug_filter_by_environment(client):
    rep = _make_user(client, email="r@example.com")
    p = _make_project(client, name="ProjA")
    _make_bug(client, p["id"], rep["id"], title="dev bug", environment="DEV")
    _make_bug(client, p["id"], rep["id"], title="prod bug", environment="PROD")

    rs = client.get("/api/bugs?environment=PROD").json()
    assert {x["title"] for x in rs["items"]} == {"prod bug"}


def test_bug_update_logs_changes(client):
    rep = _make_user(client, email="r@example.com")
    p = _make_project(client, name="ProjA")
    bug = _make_bug(client, p["id"], rep["id"], environment="DEV")

    r = client.put(f"/api/bugs/{bug['id']}", json={
        "status": "In Progress", "environment": "UAT", "actor_user_id": rep["id"],
    })
    assert r.status_code == 200
    assert r.json()["environment"] == "UAT"

    activities = client.get(f"/api/bugs/{bug['id']}/activity").json()
    actions = {a["action"] for a in activities}
    assert "status_changed" in actions
    assert "environment_changed" in actions


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
def test_comment_lifecycle(client):
    rep = _make_user(client, email="r@example.com")
    p = _make_project(client, name="ProjA")
    bug = _make_bug(client, p["id"], rep["id"])
    r = client.post(f"/api/bugs/{bug['id']}/comments",
                    json={"author_user_id": rep["id"], "body": "Reproduced"})
    assert r.status_code == 201
    assert r.json()["body"] == "Reproduced"
    cs = client.get(f"/api/bugs/{bug['id']}/comments").json()
    assert len(cs) == 1


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
def test_attachment_upload_list_download_delete(client):
    rep = _make_user(client, email="r@example.com")
    p = _make_project(client, name="ProjA")
    bug = _make_bug(client, p["id"], rep["id"])

    # Upload a fake PNG (just bytes — content-type is what matters)
    payload = b"\x89PNG\r\n\x1a\n" + b"fake png data" * 10
    r = client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("screenshot.png", io.BytesIO(payload), "image/png")},
        data={"uploader_user_id": rep["id"]},
    )
    assert r.status_code == 201, r.text
    att = r.json()
    assert att["filename"] == "screenshot.png"
    assert att["content_type"] == "image/png"
    assert att["size_bytes"] == len(payload)

    # Bug detail should now show the attachment
    detail = client.get(f"/api/bugs/{bug['id']}").json()
    assert detail["attachment_count"] == 1
    assert len(detail["attachments"]) == 1

    # Download it back
    r = client.get(f"/api/bugs/{bug['id']}/attachments/{att['id']}/download")
    assert r.status_code == 200
    assert r.content == payload
    assert r.headers["content-type"].startswith("image/png")

    # Delete
    r = client.delete(f"/api/bugs/{bug['id']}/attachments/{att['id']}?actor_user_id={rep['id']}")
    assert r.status_code == 200
    detail = client.get(f"/api/bugs/{bug['id']}").json()
    assert detail["attachment_count"] == 0


def test_attachment_too_large(client):
    rep = _make_user(client, email="r@example.com")
    p = _make_project(client, name="ProjA")
    bug = _make_bug(client, p["id"], rep["id"])

    # 51 MB > 50 MB limit
    big = b"x" * (51 * 1024 * 1024)
    r = client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("huge.bin", io.BytesIO(big), "application/octet-stream")},
    )
    assert r.status_code == 413


def test_attachment_on_comment(client):
    rep = _make_user(client, email="r@example.com")
    p = _make_project(client, name="ProjA")
    bug = _make_bug(client, p["id"], rep["id"])
    c = client.post(
        f"/api/bugs/{bug['id']}/comments",
        json={"author_user_id": rep["id"], "body": "see attached"},
    ).json()
    r = client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4 data"), "application/pdf")},
        data={"uploader_user_id": rep["id"], "comment_id": c["id"]},
    )
    assert r.status_code == 201
    assert r.json()["comment_id"] == c["id"]

    detail = client.get(f"/api/bugs/{bug['id']}").json()
    com = next(x for x in detail["comments"] if x["id"] == c["id"])
    assert len(com["attachments"]) == 1
    assert com["attachments"][0]["filename"] == "doc.pdf"


# ---------------------------------------------------------------------------
# Audit trail (global)
# ---------------------------------------------------------------------------
def test_global_audit_trail(client):
    rep = _make_user(client, email="r@example.com")
    p = _make_project(client, name="ProjA")
    bug = _make_bug(client, p["id"], rep["id"])
    client.post(f"/api/bugs/{bug['id']}/comments",
                json={"author_user_id": rep["id"], "body": "test"})

    audit = client.get("/api/audit").json()
    types = {a["entity_type"] for a in audit}
    actions = {a["action"] for a in audit}
    assert "user" in types
    assert "project" in types
    assert "bug" in types
    assert "user_created" in actions
    assert "project_created" in actions
    assert "bug_created" in actions


def test_audit_filter_by_actor(client):
    alice = _make_user(client, name="Alice", email="alice@example.com")
    p_resp = client.post("/api/projects", json={"name": "QQ", "description": "", "color": "#c9764f"},
                         params={"actor_user_id": alice["id"]})
    assert p_resp.status_code == 201

    audit = client.get(f"/api/audit?actor_user_id={alice['id']}").json()
    assert all(a["actor_user_id"] == alice["id"] for a in audit)
    assert any(a["action"] == "project_created" for a in audit)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def test_stats(client):
    rep = _make_user(client, email="r@example.com")
    p = _make_project(client, name="ProjA")
    _make_bug(client, p["id"], rep["id"], environment="UAT")
    s = client.get("/api/stats").json()
    assert s["bugs"] >= 1
    assert "by_environment" in s
    assert s["by_environment"].get("UAT", 0) >= 1
    assert "by_severity" not in s  # severity is GONE


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
def test_csv_export(client):
    rep = _make_user(client, name="Reporter R", email="r@example.com")
    p = _make_project(client, name="CSV Test")
    _make_bug(client, p["id"], rep["id"], title="csv subject", environment="PROD")
    r = client.get("/api/bugs/export.csv")
    assert r.status_code == 200
    assert "csv subject" in r.text
    assert "PROD" in r.text


# ---------------------------------------------------------------------------
# User deletion FK cascade
# ---------------------------------------------------------------------------
def test_deleting_user_cleans_up_assignees_and_nulls_reporter(client):
    rep = _make_user(client, name="Rep", email="r@example.com")
    alice = _make_user(client, name="Alice", email="alice@example.com")
    p = _make_project(client, name="ProjA")
    bug = _make_bug(client, p["id"], rep["id"], assignee_ids=[alice["id"]])

    assert client.delete(f"/api/users/{alice['id']}").status_code == 200
    fresh = client.get(f"/api/bugs/{bug['id']}").json()
    assert fresh["assignees"] == []

    assert client.delete(f"/api/users/{rep['id']}").status_code == 200
    fresh = client.get(f"/api/bugs/{bug['id']}").json()
    assert fresh["reporter"] is None
