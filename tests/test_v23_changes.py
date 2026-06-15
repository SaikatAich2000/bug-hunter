"""Regression tests for the v2.3 changes:

  1. Type-based permission tightening — users can't edit/delete tasks or
     requirements; managers can edit but never delete.
  2. Event managers (admin/manager users) receive event-level emails
     (create / update / delete) — but NOT per-task emails.
  3. Only admin/manager roles allowed as event managers.
  4. Event delete is admin-only (managers can edit but not delete).
  5. Manager validation: trying to add a regular user as event manager
     returns 400.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _login(client, email, password):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _make_user(client, name, role="user", email=None):
    email = email or f"{name.lower()}@x.test"
    r = client.post("/api/users", json={
        "name": name, "email": email, "role": role,
        "password": "User12345Aa",
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
# 1. Type-based permission tightening
# ---------------------------------------------------------------------------
def test_user_can_edit_bug_but_not_task_or_requirement(admin_client):
    # Admin creates the three flavors of items under their own session.
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    req = _make_item(admin_client, p["id"], item_type="Requirement")
    task = _make_item(admin_client, p["id"], item_type="Task")
    _make_user(admin_client, "Regular", role="user")
    _login(admin_client, "regular@x.test", "User12345Aa")

    # Bug edit: ALLOWED (legacy behaviour, every user can touch a bug).
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "In Progress"})
    assert r.status_code == 200, r.text

    # Requirement edit: FORBIDDEN for regular users.
    r = admin_client.put(f"/api/bugs/{req['id']}", json={"status": "In Progress"})
    assert r.status_code == 403, r.text
    assert "requirement" in r.json()["detail"].lower()

    # Task edit: FORBIDDEN for regular users.
    r = admin_client.put(f"/api/bugs/{task['id']}", json={"status": "In Progress"})
    assert r.status_code == 403, r.text
    assert "task" in r.json()["detail"].lower()


def test_user_cannot_convert_bug_to_task_via_item_type_overpost(admin_client):
    """Mass-assignment guard: a regular user may edit a Bug, but must not be
    able to reclassify it into a Task/Requirement (a type they cannot edit) by
    over-posting item_type. The PUT is rejected 403 and the row is unchanged."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    _make_user(admin_client, "Regular", role="user")
    _login(admin_client, "regular@x.test", "User12345Aa")

    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"item_type": "Task"})
    assert r.status_code == 403, r.text
    assert "convert" in r.json()["detail"].lower()

    # Still a Bug — the guard rejects before any write (no partial update).
    got = admin_client.get(f"/api/bugs/{bug['id']}")
    assert got.status_code == 200, got.text
    assert got.json()["item_type"] == "Bug"


def test_manager_can_edit_task_and_requirement(admin_client):
    p = _make_project(admin_client)
    task = _make_item(admin_client, p["id"], item_type="Task")
    req = _make_item(admin_client, p["id"], item_type="Requirement")
    _make_user(admin_client, "Mgr", role="manager")
    _login(admin_client, "mgr@x.test", "User12345Aa")
    # "In Progress" is a Task-valid status (kept the original test
    # value here so the Task assertion still exercises the same status
    # transition pre/post-v2.5).
    r = admin_client.put(f"/api/bugs/{task['id']}", json={"status": "In Progress"})
    assert r.status_code == 200, r.text
    # v2.5: Requirements no longer share Bug-only statuses. The original
    # test used "Resolved" which is now Bug-only — switch to "Approved"
    # which is the Requirement-flavor equivalent.
    r = admin_client.put(f"/api/bugs/{req['id']}", json={"status": "Approved"})
    assert r.status_code == 200, r.text


def test_manager_cannot_delete_anything(admin_client):
    """Managers can edit, never delete — every item type AND events."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    task = _make_item(admin_client, p["id"], item_type="Task")
    req = _make_item(admin_client, p["id"], item_type="Requirement")
    ev = admin_client.post("/api/events", json={
        "name": "for-delete-test",
    }).json()
    _make_user(admin_client, "Mgr2", role="manager")
    _login(admin_client, "mgr2@x.test", "User12345Aa")

    for bid in (bug["id"], task["id"], req["id"]):
        r = admin_client.delete(f"/api/bugs/{bid}")
        assert r.status_code == 403, f"Manager should NOT be able to delete bug #{bid}, got {r.status_code}"
    r = admin_client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 403, "Manager should not be able to delete events"


def test_admin_can_delete_everything(admin_client):
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    task = _make_item(admin_client, p["id"], item_type="Task")
    ev = admin_client.post("/api/events", json={"name": "delete-me"}).json()
    for bid in (bug["id"], task["id"]):
        r = admin_client.delete(f"/api/bugs/{bid}")
        assert r.status_code == 200, r.text
    r = admin_client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 2 + 3 + 5. Event managers — emails, role validation
# ---------------------------------------------------------------------------
def test_event_create_emails_only_managers(admin_client, monkeypatch):
    """Creating an event sends email to its managers — NOT to its items'
    assignees, NOT to random users."""
    # Make manager users.
    m1 = _make_user(admin_client, "Mgr-a", role="manager", email="mgra@x.test")
    m2 = _make_user(admin_client, "Mgr-b", role="manager", email="mgrb@x.test")
    # And a regular user we'll later assign to a task inside the event.
    _make_user(admin_client, "Worker", role="user", email="worker@x.test")

    sent = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body: sent.append((subject, sorted(to), body)),
    )

    sent.clear()
    ev = admin_client.post("/api/events", json={
        "name": "Sprint kickoff",
        "manager_ids": [m1["id"], m2["id"]],
    }).json()
    # The admin is the actor and is excluded from recipients. Only the
    # two managers should be addressed.
    assert sent, "Expected an event-created email"
    subj, to, body = sent[-1]
    assert "New event" in subj
    assert "Sprint kickoff" in subj
    assert to == ["mgra@x.test", "mgrb@x.test"], to
    assert "Managers:" in body
    assert ev["managers"] and len(ev["managers"]) == 2


def test_event_update_emails_managers(admin_client, monkeypatch):
    mgr = _make_user(admin_client, "Mgr3", role="manager", email="m3@x.test")
    sent = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body: sent.append((subject, sorted(to), body)),
    )
    ev = admin_client.post("/api/events", json={
        "name": "Daily", "manager_ids": [mgr["id"]],
    }).json()
    sent.clear()
    r = admin_client.put(f"/api/events/{ev['id']}", json={"name": "Daily (renamed)"})
    assert r.status_code == 200
    assert sent, "Expected an event-updated email"
    subj, to, body = sent[-1]
    assert "updated" in subj.lower()
    assert "m3@x.test" in to
    assert "name" in body  # change line is present


def test_event_delete_emails_managers(admin_client, monkeypatch):
    mgr = _make_user(admin_client, "Mgr4", role="manager", email="m4@x.test")
    sent = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body: sent.append((subject, sorted(to), body)),
    )
    ev = admin_client.post("/api/events", json={
        "name": "doomed", "manager_ids": [mgr["id"]],
    }).json()
    sent.clear()
    r = admin_client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 200
    assert sent
    subj, to, body = sent[-1]
    assert "deleted" in subj.lower()
    assert "m4@x.test" in to


def test_task_creation_does_NOT_email_event_managers(admin_client, monkeypatch):
    """The key promise: dropping a task inside an event should NOT
    notify the event's managers — only the task's own assignees."""
    mgr = _make_user(admin_client, "EvMgr", role="manager", email="evmgr@x.test")
    worker = _make_user(admin_client, "Wkr", role="user", email="wkr@x.test")
    p = _make_project(admin_client)
    ev = admin_client.post("/api/events", json={
        "name": "Standup", "manager_ids": [mgr["id"]],
    }).json()
    sent = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body: sent.append((subject, sorted(to), body)),
    )
    # File a task inside the event, assigned to worker only.
    r = admin_client.post("/api/bugs", json={
        "title": "Do the thing",
        "project_id": p["id"], "item_type": "Task",
        "event_id": ev["id"], "assignee_ids": [worker["id"]],
    })
    assert r.status_code == 201, r.text
    # We expect ONE bug-created email (to the assignee). The event
    # manager's address must not appear anywhere.
    flat = " ".join(b for _, _, b in sent) + " " + " ".join(s for s, _, _ in sent)
    all_to = [addr for _, to, _ in sent for addr in to]
    assert "wkr@x.test" in all_to, "Assignee should be notified"
    assert "evmgr@x.test" not in all_to, \
        "Event manager must NOT be cc'd on task-created emails"
    # Sanity: the email subject is task-typed, not bug-typed.
    assert any("task" in s.lower() for s, _, _ in sent)


def test_event_managers_must_be_admin_or_manager(admin_client):
    """Regular users can't be event managers. Returns 400 with a helpful detail."""
    regular = _make_user(admin_client, "Plain", role="user", email="plain@x.test")
    r = admin_client.post("/api/events", json={
        "name": "bad-managers", "manager_ids": [regular["id"]],
    })
    assert r.status_code == 400, r.text
    assert "manager" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 4. Event delete admin-only — covered by test_manager_cannot_delete_anything,
# also sanity-check that a regular user is forbidden too.
# ---------------------------------------------------------------------------
def test_regular_user_cannot_delete_event(admin_client):
    ev = admin_client.post("/api/events", json={"name": "user-delete-test"}).json()
    _make_user(admin_client, "User9", role="user", email="u9@x.test")
    _login(admin_client, "u9@x.test", "User12345Aa")
    r = admin_client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 403


def test_regular_user_cannot_create_event(admin_client):
    _make_user(admin_client, "User10", role="user", email="u10@x.test")
    _login(admin_client, "u10@x.test", "User12345Aa")
    r = admin_client.post("/api/events", json={"name": "should-fail"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Event detail still works after managers added
# ---------------------------------------------------------------------------
def test_event_out_includes_managers(admin_client):
    mgr = _make_user(admin_client, "Mgr5", role="manager", email="m5@x.test")
    ev = admin_client.post("/api/events", json={
        "name": "with-managers", "manager_ids": [mgr["id"]],
    }).json()
    assert len(ev["managers"]) == 1
    assert ev["managers"][0]["name"] == "Mgr5"
    # Detail endpoint too.
    detail = admin_client.get(f"/api/events/{ev['id']}").json()
    assert len(detail["managers"]) == 1
    assert detail["managers"][0]["email"] == "m5@x.test"


# ---------------------------------------------------------------------------
# Tab-aware /api/stats — every aggregation filters on item_type when set,
# but by_type stays global so the tab badges stay correct.
# ---------------------------------------------------------------------------
def test_stats_filters_kpis_by_item_type(admin_client):
    p = _make_project(admin_client)
    # 3 bugs, 1 requirement, 2 tasks.
    for i in range(3):
        _make_item(admin_client, p["id"], item_type="Bug", title=f"Bug-{i}-name")
    _make_item(admin_client, p["id"], item_type="Requirement", title="Req-0-name")
    for i in range(2):
        _make_item(admin_client, p["id"], item_type="Task", title=f"Task-{i}-name")

    # Global: total counts everything.
    global_s = admin_client.get("/api/stats").json()
    assert global_s["bugs"] == 6  # all non-excluded statuses
    assert global_s["by_type"]["Bug"] == 3
    assert global_s["by_type"]["Requirement"] == 1
    assert global_s["by_type"]["Task"] == 2

    # Bug tab: counts shift to bugs only.
    bug_s = admin_client.get("/api/stats?item_type=Bug").json()
    assert bug_s["bugs"] == 3
    assert bug_s["open"] == 3        # default status is "New"
    # by_type stays GLOBAL even when filtered — tab badges must keep
    # showing reality.
    assert bug_s["by_type"]["Bug"] == 3
    assert bug_s["by_type"]["Requirement"] == 1
    assert bug_s["by_type"]["Task"] == 2

    # Task tab: counts shift to tasks only.
    task_s = admin_client.get("/api/stats?item_type=Task").json()
    assert task_s["bugs"] == 2
    assert task_s["open"] == 2

    # Requirement tab.
    req_s = admin_client.get("/api/stats?item_type=Requirement").json()
    assert req_s["bugs"] == 1


def test_stats_filters_breakdowns_by_item_type(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], item_type="Bug",         priority="High")
    _make_item(admin_client, p["id"], item_type="Bug",         priority="High")
    _make_item(admin_client, p["id"], item_type="Task",        priority="Low")
    _make_item(admin_client, p["id"], item_type="Requirement", priority="Medium")

    bug_s = admin_client.get("/api/stats?item_type=Bug").json()
    # by_priority should only see bug rows.
    assert bug_s["by_priority"].get("High") == 2
    assert "Low" not in bug_s["by_priority"]    # task's Low excluded
    assert "Medium" not in bug_s["by_priority"]  # req's Medium excluded

    task_s = admin_client.get("/api/stats?item_type=Task").json()
    assert task_s["by_priority"].get("Low") == 1
    assert "High" not in task_s["by_priority"]


def test_stats_rejects_unknown_item_type(admin_client):
    r = admin_client.get("/api/stats?item_type=Bogus")
    assert r.status_code == 400
    assert "item_type" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Audit-trail preservation — deleting a bug must NOT wipe its history
# ---------------------------------------------------------------------------
def test_audit_history_survives_bug_delete(admin_client):
    """Pre-fix behavior: cascade deletes ate every activity row for the bug
    along with the bug. After fix: we detach the rows (bug_id NULL) before
    delete so the global trail keeps the full history."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug", title="Login broken thing")
    # Generate a real history: edit it, leave a comment, then delete.
    admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "In Progress"})
    admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "Investigating"})
    audit_before = admin_client.get("/api/audit").json()
    rows_for_bug_before = [
        r for r in audit_before
        if (r["entity_type"] == "bug" and r["entity_id"] == bug["id"])
        or f"#{bug['id']}" in (r["detail"] or "")
    ]
    assert len(rows_for_bug_before) >= 3, rows_for_bug_before  # create + status + comment

    # Delete the bug — admin only.
    r = admin_client.delete(f"/api/bugs/{bug['id']}")
    assert r.status_code == 200, r.text

    audit_after = admin_client.get("/api/audit").json()
    rows_for_bug_after = [
        r for r in audit_after
        if (r["entity_type"] == "bug" and r["entity_id"] == bug["id"])
        or f"#{bug['id']}" in (r["detail"] or "")
    ]
    # The full history should still be there, PLUS the new "bug_deleted" row.
    actions = [r["action"] for r in rows_for_bug_after]
    assert "bug_created" in actions, actions
    assert "comment_added" in actions, actions
    assert "status_changed" in actions, actions
    assert "bug_deleted" in actions, actions


def test_audit_search_by_bug_title(admin_client):
    """Searching the audit trail by bug title should hit history rows for
    that bug — both via the live bug.title (LEFT JOIN) and via the title
    baked into the detail string when the row was written."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug", title="Payment gateway timeout")
    admin_client.put(f"/api/bugs/{bug['id']}", json={"priority": "Critical"})

    r = admin_client.get("/api/audit?q=Payment+gateway")
    assert r.status_code == 200
    rows = r.json()
    assert any(r["entity_id"] == bug["id"] for r in rows), \
        "Searching by bug title should find audit rows for that bug"


def test_audit_search_by_item_type_word(admin_client):
    """Typing 'task' should narrow to task-related audit events. We have
    the item type both in the joined Bug.item_type and in the
    bug_created detail string."""
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], item_type="Task", title="Write the spec")
    _make_item(admin_client, p["id"], item_type="Bug",  title="Crash on submit")
    r = admin_client.get("/api/audit?q=task")
    rows = r.json()
    # At least one row should mention/reference the task.
    assert any(
        "task" in (row["detail"] or "").lower() or
        "task" in (row["action"] or "").lower()
        for row in rows
    ), rows


def test_audit_search_by_assignee_name(admin_client):
    """Assignment audit detail bakes the assignee names in. Searching by
    a name should find the assignment event."""
    user = _make_user(admin_client, "Sandra", role="user", email="sandra@x.test")
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug",
                     title="Crash on submit form", assignee_ids=[user["id"]])
    r = admin_client.get("/api/audit?q=Sandra")
    rows = r.json()
    assert any(r["entity_id"] == bug["id"] for r in rows), \
        f"Searching audit by assignee name 'Sandra' should hit bug #{bug['id']}: got {rows[:3]}"
