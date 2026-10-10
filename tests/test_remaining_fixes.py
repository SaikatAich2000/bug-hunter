"""Regression tests for the remaining-fixes batch (user delete audit, seed boot,
session cookie, upload allowlist, lockout scope, 422 shape, documented statuses)."""
from __future__ import annotations


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
