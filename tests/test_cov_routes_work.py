"""Coverage slice: app.routes.events and app.routes.bugs (work-item routes).

This file targets specific uncovered branches/lines in the events and bugs
route modules, driving them through the public API wherever possible and
falling back to direct calls of the route module's pure helper functions
(rate-limit guard, event brief with a null creator) only where an HTTP path
can't reach the line hermetically.

Everything here is hermetic — email/push/HIBP stay disabled by the `client`
fixture and we never make a real network call. Modules are imported INSIDE
each test because the `client` fixture deletes and re-imports every ``app.*``
module per test (see tests/conftest.py / tests/test_password_policy.py), so a
top-level import would go stale.
"""
from __future__ import annotations

import io


# ---------------------------------------------------------------------------
# Helpers (mirrors the patterns in tests/test_events.py & tests/test_api.py)
# ---------------------------------------------------------------------------
def _make_project(client, name="Eng"):
    r = client.post("/api/projects", json={"name": name, "color": "#c9764f"})
    assert r.status_code == 201, r.text
    return r.json()


def _make_event(client, name="Standup 2026-05-28", **extra):
    body = {"name": name, "scheduled_for": "2026-05-28"}
    body.update(extra)
    r = client.post("/api/events", json=body)
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


def _make_user(client, name, role="user", email=None, password="User12345Aa"):
    email = email or f"{name.lower()}@cov.test"
    r = client.post("/api/users", json={
        "name": name, "email": email, "role": role, "password": password,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _login(client, email, password):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


# ===========================================================================
# events.py
# ===========================================================================

# --- 231: list_events filtered by scheduled_for --------------------------
def test_cov_events_list_filter_by_scheduled_for(admin_client):
    _make_event(admin_client, name="On 28th", scheduled_for="2026-05-28")
    _make_event(admin_client, name="On 29th", scheduled_for="2026-05-29")
    rows = admin_client.get("/api/events?scheduled_for=2026-05-28").json()
    names = {r["name"] for r in rows}
    assert "On 28th" in names
    assert "On 29th" not in names


# --- 180: _resolve_managers rejects unknown user ids ---------------------
def test_cov_events_create_unknown_manager_id_400(admin_client):
    r = admin_client.post("/api/events", json={
        "name": "bad-mgr", "manager_ids": [999999],
    })
    assert r.status_code == 400, r.text
    assert "unknown user ids" in r.json()["detail"].lower()


# --- 362-374: _apply_event_manager_diff when the manager set changes ------
def test_cov_events_update_changes_manager_set(admin_client):
    m1 = _make_user(admin_client, "MgrA", role="manager", email="mgra@cov.test")
    m2 = _make_user(admin_client, "MgrB", role="manager", email="mgrb@cov.test")
    ev = _make_event(admin_client, name="mgr-swap", manager_ids=[m1["id"]])
    assert [m["id"] for m in ev["managers"]] == [m1["id"]]
    # Replace the manager list — old != new triggers the diff branch.
    r = admin_client.put(f"/api/events/{ev['id']}", json={"manager_ids": [m2["id"]]})
    assert r.status_code == 200, r.text
    assert {m["id"] for m in r.json()["managers"]} == {m2["id"]}
    # The change is audited as a "managers" field change.
    audit = admin_client.get("/api/audit").json()
    assert any(a["action"] == "event_managers_changed" for a in audit), audit


def test_cov_events_update_same_manager_set_is_noop(admin_client):
    """Re-sending the identical manager list (different order) is NOT a change
    — exercises the early-return at the top of _apply_event_manager_diff."""
    m1 = _make_user(admin_client, "MgrC", role="manager", email="mgrc@cov.test")
    m2 = _make_user(admin_client, "MgrD", role="manager", email="mgrd@cov.test")
    ev = _make_event(admin_client, name="mgr-same", manager_ids=[m1["id"], m2["id"]])
    r = admin_client.put(f"/api/events/{ev['id']}", json={
        "manager_ids": [m2["id"], m1["id"]],  # same set, reversed
    })
    assert r.status_code == 200, r.text
    assert {m["id"] for m in r.json()["managers"]} == {m1["id"], m2["id"]}


# --- 380-381: _persist_event_update rollback when there are no changes ----
def test_cov_events_update_no_change_rolls_back(admin_client):
    ev = _make_event(admin_client, name="No-op event")
    audit_before = admin_client.get("/api/audit").json()
    n_before = sum(1 for a in audit_before if a.get("entity_type") == "event")
    # PUT the same name → _compute_event_changes finds nothing, manager_ids
    # is omitted, so changes stays empty and _persist_event_update rolls back.
    r = admin_client.put(f"/api/events/{ev['id']}", json={"name": "No-op event"})
    assert r.status_code == 200, r.text
    audit_after = admin_client.get("/api/audit").json()
    n_after = sum(1 for a in audit_after if a.get("entity_type") == "event")
    # No new event-field-change audit rows were written.
    assert n_after == n_before


# --- 401: update_event 404 -----------------------------------------------
def test_cov_events_update_missing_404(admin_client):
    r = admin_client.put("/api/events/999999", json={"name": "ghost"})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Event not found"


# --- 444: delete_event 404 -----------------------------------------------
def test_cov_events_delete_missing_404(admin_client):
    r = admin_client.delete("/api/events/999999")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Event not found"


# --- 124->130: _event_brief when the event has no creator ----------------
def test_cov_events_brief_with_null_creator(admin_client):
    """An event whose created_by_user_id is NULL (e.g. seeded directly, or
    whose creator was later removed and the FK SET NULL) must still render.
    Drives the false branch of `if ev.created_by_user_id:` in _event_brief.

    Seeding a row directly on the app's own session is hermetic test setup;
    it doesn't touch app/ source. We use the SAME SessionLocal the app uses,
    imported after the client fixture has booted the app for this test."""
    from app.database import SessionLocal
    from app.models import Event

    db = SessionLocal()
    try:
        ev = Event(name="Orphan event", description="", scheduled_for=None,
                   created_by_user_id=None)
        db.add(ev)
        db.commit()
        db.refresh(ev)
        ev_id = ev.id
    finally:
        db.close()

    # Detail view calls _event_brief; the null creator means created_by_name
    # stays None (the 124->130 jump).
    detail = admin_client.get(f"/api/events/{ev_id}").json()
    assert detail["id"] == ev_id
    assert detail["created_by_user_id"] is None
    assert detail["created_by_name"] is None
    # List view also renders it without error.
    rows = admin_client.get("/api/events").json()
    assert any(r["id"] == ev_id and r["created_by_name"] is None for r in rows)


# ===========================================================================
# bugs.py
# ===========================================================================

# --- 88 & 92: _check_user_rate internals (cap eviction + expiry popleft) ---
def test_cov_bugs_rate_guard_cap_eviction_and_expiry(client):
    """Directly exercises the sliding-window rate guard's two inner branches:
      - line 88: bucket dict at capacity → evict the oldest user's bucket
      - line 92: a timestamp older than the window cutoff is popped
    These are in-process, deterministic, and can't be reached cleanly over
    HTTP without hammering 20+ real uploads, so we unit-test the helper.
    `client` is requested only so app.* is freshly imported for this test."""
    import threading
    import time

    import app.routes.bugs as bugs

    # --- line 88: cap eviction. Pre-fill the dict to its cap with a dummy
    # user, then a request from a NEW user must evict the oldest entry.
    buckets: dict = {}
    lock = threading.Lock()
    bugs._check_user_rate(
        buckets, lock, user_id=1, max_req=5, window=60,
        detail="x", cap=1,
    )
    assert 1 in buckets and len(buckets) == 1
    # New user while at cap → oldest (user 1) evicted, user 2 inserted.
    bugs._check_user_rate(
        buckets, lock, user_id=2, max_req=5, window=60,
        detail="x", cap=1,
    )
    assert 2 in buckets
    assert 1 not in buckets  # evicted

    # --- line 92: expiry popleft. Seed a bucket with a stale timestamp that
    # is older than the window cutoff; the next call must pop it.
    from collections import deque
    buckets2: dict = {7: deque([time.monotonic() - 120.0])}
    bugs._check_user_rate(
        buckets2, lock, user_id=7, max_req=5, window=60,
        detail="x", cap=100,
    )
    # The stale entry was popped and exactly one fresh entry remains.
    assert len(buckets2[7]) == 1


# --- 238: _resolve_user raises 400 for a non-existent reporter -----------
def test_cov_bugs_update_unknown_reporter_400(admin_client):
    p = _make_project(admin_client, name="ReporterProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    # Admin passes the reporter-change gate; _apply_reporter_change then calls
    # _resolve_user(999999) which raises 400.
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"reporter_id": 999999})
    assert r.status_code == 400, r.text
    assert "does not exist" in r.json()["detail"].lower()


# --- 312: _apply_q_filter early-return when the cleaned query is empty -----
def test_cov_bugs_list_q_only_hash_returns_all(admin_client):
    p = _make_project(admin_client, name="QProj")
    _make_item(admin_client, p["id"], item_type="Bug", title="alpha")
    _make_item(admin_client, p["id"], item_type="Bug", title="beta")
    # "#" strips to "" → not a digit, not truthy → filter is a no-op.
    rows = admin_client.get("/api/bugs?q=%23").json()
    assert rows["total"] >= 2


# --- 346: reporter_id list filter ----------------------------------------
def test_cov_bugs_list_filter_by_reporter_id(admin_client):
    me = admin_client.get("/api/auth/me").json()
    u = _make_user(admin_client, "Reporter1", role="manager", email="rep1@cov.test")
    p = _make_project(admin_client, name="RepFilterProj")
    # One bug reported by the admin, one reported by the other user.
    _make_item(admin_client, p["id"], item_type="Bug", title="admins")
    _make_item(admin_client, p["id"], item_type="Bug", title="theirs",
               reporter_id=u["id"])
    rows = admin_client.get(f"/api/bugs?reporter_id={u['id']}").json()
    assert rows["total"] == 1
    assert rows["items"][0]["reporter"]["id"] == u["id"]
    # And the admin's own filter returns the other one (sanity).
    mine = admin_client.get(f"/api/bugs?reporter_id={me['id']}").json()
    assert all(it["reporter"]["id"] == me["id"] for it in mine["items"])
    assert mine["total"] >= 1


# --- 659: _validate_update_payload rejects an unknown project_id ----------
def test_cov_bugs_update_unknown_project_400(admin_client):
    p = _make_project(admin_client, name="UpdProjA")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"project_id": 999999})
    assert r.status_code == 400, r.text
    assert "project does not exist" in r.json()["detail"].lower()


# --- 755: update_bug 404 --------------------------------------------------
def test_cov_bugs_update_missing_404(admin_client):
    r = admin_client.put("/api/bugs/999999", json={"title": "ghost title"})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Bug not found"


# --- 836: delete_bug 404 --------------------------------------------------
def test_cov_bugs_delete_missing_404(admin_client):
    r = admin_client.delete("/api/bugs/999999")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Bug not found"


# --- 887-897: list_comments with a comment-level attachment ---------------
def test_cov_bugs_list_comments_with_attachment(admin_client):
    p = _make_project(admin_client, name="CommentsProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    # Two comments so the ordering/grouping loop runs over multiple rows.
    c1 = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "first"})
    assert c1.status_code == 201, c1.text
    c2 = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "second"})
    assert c2.status_code == 201, c2.text
    # Attach a file to the second comment so by_cid grouping has an entry.
    files = {"file": ("note.txt", io.BytesIO(b"attached bytes"), "text/plain")}
    r = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files=files, data={"comment_id": str(c2.json()["id"])},
    )
    assert r.status_code == 201, r.text

    listing = admin_client.get(f"/api/bugs/{bug['id']}/comments")
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert len(rows) == 2
    by_id = {c["id"]: c for c in rows}
    # The attachment is grouped under its comment; the other has none.
    assert len(by_id[c2.json()["id"]]["attachments"]) == 1
    assert by_id[c2.json()["id"]]["attachments"][0]["filename"] == "note.txt"
    assert by_id[c1.json()["id"]]["attachments"] == []


# --- 968: _read_upload_with_limit aborts oversize uploads with 413 --------
def test_cov_bugs_upload_too_large_413(admin_client, monkeypatch):
    """Shrink the per-file cap to a few bytes (instead of streaming a real
    50 MB body) so a tiny upload trips the 413 guard in _read_upload_with_limit.
    The module global is read at call time, so a monkeypatch on it takes
    effect for the request."""
    import app.routes.bugs as bugs
    monkeypatch.setattr(bugs, "MAX_FILE_BYTES", 4)

    p = _make_project(admin_client, name="BigFileProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    files = {"file": ("big.bin", io.BytesIO(b"way too many bytes"), "application/octet-stream")}
    r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
    assert r.status_code == 413, r.text
    assert "too large" in r.json()["detail"].lower()


# --- 990: upload_attachment 404 when the bug doesn't exist ----------------
def test_cov_bugs_upload_missing_bug_404(admin_client):
    files = {"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")}
    r = admin_client.post("/api/bugs/999999/attachments", files=files)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Bug not found"


# --- 1087: delete_attachment 404 -----------------------------------------
def test_cov_bugs_delete_attachment_missing_404(admin_client):
    p = _make_project(admin_client, name="DelAttProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    # Attachment id 999999 doesn't exist for this (real) bug → 404.
    r = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/999999")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Attachment not found"


# --- 1129: update_comment 404 (admin authorised, comment absent) ----------
def test_cov_bugs_update_comment_missing_404(admin_client):
    p = _make_project(admin_client, name="UpdCmtProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.put(
        f"/api/bugs/{bug['id']}/comments/999999", json={"body": "edited"},
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Comment not found"


# --- 1166: delete_comment 404 (admin authorised, comment absent) ----------
def test_cov_bugs_delete_comment_missing_404(admin_client):
    p = _make_project(admin_client, name="DelCmtProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.delete(f"/api/bugs/{bug['id']}/comments/999999")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Comment not found"


# --- 1188: list_activity 404 when the bug doesn't exist -------------------
def test_cov_bugs_list_activity_missing_bug_404(admin_client):
    r = admin_client.get("/api/bugs/999999/activity")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Bug not found"


# ---------------------------------------------------------------------------
# A couple of happy-path status transitions per item_type so the per-type
# status validation path (statuses_for_type) is exercised across flavors and
# a valid transition for each type is observed end-to-end.
# ---------------------------------------------------------------------------
def test_cov_bugs_status_transitions_per_item_type(admin_client):
    p = _make_project(admin_client, name="StatusProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    task = _make_item(admin_client, p["id"], item_type="Task")
    req = _make_item(admin_client, p["id"], item_type="Requirement")

    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "Resolved"})
    assert r.status_code == 200 and r.json()["status"] == "Resolved"
    r = admin_client.put(f"/api/bugs/{task['id']}", json={"status": "Done"})
    assert r.status_code == 200 and r.json()["status"] == "Done"
    r = admin_client.put(f"/api/bugs/{req['id']}", json={"status": "Approved"})
    assert r.status_code == 200 and r.json()["status"] == "Approved"

    # A status from the wrong type's set is rejected (Task can't be "Resolved").
    r = admin_client.put(f"/api/bugs/{task['id']}", json={"status": "Resolved"})
    assert r.status_code == 400, r.text
    assert "not valid for task" in r.json()["detail"].lower()
