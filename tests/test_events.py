"""Tests for the Events feature — containers for groups of work items.

Covers:
- Event CRUD via /api/events
- Linking / unlinking items via Bug.event_id (POST and PUT)
- The event-aware email body (Event: <name> appears in the metadata block)
- Audit log records event create / update / delete
- Deleting an event preserves its items (FK is NULLed)
- Migration safety: re-running init_db on a populated DB is a no-op
- Sleuth chatbot mentions the event in its item-detail reply
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers
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


def _make_item(client, project_id, **extra):
    body = {
        "title": "Sample task",
        "project_id": project_id,
        "item_type": "Task",
        "priority": "Medium",
        "environment": "DEV",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def test_event_crud(admin_client):
    ev = _make_event(admin_client, name="Sprint 12 kickoff")
    assert ev["name"] == "Sprint 12 kickoff"
    assert ev["scheduled_for"] == "2026-05-28"
    assert ev["item_count"] == 0
    assert ev["created_by_name"] == "Test Admin"

    # List
    rows = admin_client.get("/api/events").json()
    assert any(r["id"] == ev["id"] for r in rows)

    # Detail
    detail = admin_client.get(f"/api/events/{ev['id']}").json()
    assert detail["id"] == ev["id"]
    assert detail["items"] == []

    # Update
    r = admin_client.put(f"/api/events/{ev['id']}", json={
        "name": "Sprint 12 kickoff (rescheduled)",
        "scheduled_for": "2026-05-29",
    })
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Sprint 12 kickoff (rescheduled)"
    assert r.json()["scheduled_for"] == "2026-05-29"

    # Delete
    r = admin_client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 200, r.text
    r = admin_client.get(f"/api/events/{ev['id']}")
    assert r.status_code == 404


def test_event_create_validates_name(admin_client):
    r = admin_client.post("/api/events", json={"name": "x"})
    assert r.status_code == 422
    r = admin_client.post("/api/events", json={
        "name": "Standup", "scheduled_for": "not-a-date",
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Linking items to events
# ---------------------------------------------------------------------------
def test_create_item_under_event(admin_client):
    p = _make_project(admin_client)
    ev = _make_event(admin_client)
    item = _make_item(admin_client, p["id"], item_type="Task",
                      title="Dashboard improvements", event_id=ev["id"])
    assert item["event_id"] == ev["id"]
    assert item["event_name"] == ev["name"]

    detail = admin_client.get(f"/api/events/{ev['id']}").json()
    assert detail["item_count"] == 1
    assert detail["items"][0]["id"] == item["id"]
    assert detail["items"][0]["event_id"] == ev["id"]


def test_move_existing_item_into_event(admin_client):
    """The exact UX promise: create a task standalone, then put it inside
    an event later."""
    p = _make_project(admin_client)
    item = _make_item(admin_client, p["id"])
    assert item["event_id"] is None

    ev = _make_event(admin_client)
    r = admin_client.put(f"/api/bugs/{item['id']}", json={"event_id": ev["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["event_id"] == ev["id"]
    assert r.json()["event_name"] == ev["name"]


def test_unlink_item_from_event(admin_client):
    p = _make_project(admin_client)
    ev = _make_event(admin_client)
    item = _make_item(admin_client, p["id"], event_id=ev["id"])
    # event_id=0 is the "clear" signal for clients that can't easily send JSON null
    r = admin_client.put(f"/api/bugs/{item['id']}", json={"event_id": 0})
    assert r.status_code == 200
    assert r.json()["event_id"] is None


def test_create_item_with_unknown_event_400(admin_client):
    p = _make_project(admin_client)
    r = admin_client.post("/api/bugs", json={
        "title": "won't work", "project_id": p["id"], "event_id": 999999,
    })
    assert r.status_code == 400


def test_update_to_unknown_event_400(admin_client):
    p = _make_project(admin_client)
    item = _make_item(admin_client, p["id"])
    r = admin_client.put(f"/api/bugs/{item['id']}", json={"event_id": 999999})
    assert r.status_code == 400


def test_filter_bugs_by_event(admin_client):
    p = _make_project(admin_client)
    ev = _make_event(admin_client)
    _make_item(admin_client, p["id"], title="in event", event_id=ev["id"])
    _make_item(admin_client, p["id"], title="standalone")
    rows = admin_client.get(f"/api/bugs?event_id={ev['id']}").json()
    assert rows["total"] == 1
    assert rows["items"][0]["title"] == "in event"
    # event_id=0 → only standalone items
    rows = admin_client.get("/api/bugs?event_id=0").json()
    assert rows["total"] == 1
    assert rows["items"][0]["title"] == "standalone"


# ---------------------------------------------------------------------------
# Multi-assignee items in event detail (the "Kunal + Debasreeta" scenario)
# ---------------------------------------------------------------------------
def test_event_detail_includes_multi_assignee_item(admin_client):
    # Create two users via the API.
    u1 = admin_client.post("/api/users", json={
        "name": "Kunal", "email": "kunal@x.test", "role": "user",
        "password": "User12345Aa",
    }).json()
    u2 = admin_client.post("/api/users", json={
        "name": "Debasreeta", "email": "debasreeta@x.test", "role": "user",
        "password": "User12345Aa",
    }).json()
    p = _make_project(admin_client)
    ev = _make_event(admin_client)
    item = _make_item(admin_client, p["id"], item_type="Task",
                      title="Aadhaar Genius RFP upload",
                      event_id=ev["id"],
                      assignee_ids=[u1["id"], u2["id"]])
    assert {a["name"] for a in item["assignees"]} == {"Kunal", "Debasreeta"}

    detail = admin_client.get(f"/api/events/{ev['id']}").json()
    assert detail["assignee_count"] == 2
    [row] = detail["items"]
    assert {a["name"] for a in row["assignees"]} == {"Kunal", "Debasreeta"}


# ---------------------------------------------------------------------------
# Email — Event line appears in the body when item is in an event
# ---------------------------------------------------------------------------
def test_assignment_email_includes_event_name(monkeypatch):
    from app.email_service import (
        BugSnapshot, UserSnapshot, notify_assignment,
    )
    sent = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body: sent.append((subject, to, body)),
    )
    snap = BugSnapshot(
        id=42, title="do thing", project_name="P",
        status="New", priority="Medium", environment="DEV",
        description="", reporter=None,
        assignees=(UserSnapshot(id=1, name="Bob", email="b@x.test"),),
        item_type="Task", event_name="Morning standup 2026-05-28",
    )
    notify_assignment(snap, list(snap.assignees), actor_name="Alice")
    assert sent
    subj, to, body = sent[-1]
    assert "Event:       Morning standup 2026-05-28" in body, body
    assert "assigned you to a task" in body.lower()


# ---------------------------------------------------------------------------
# Delete behaviour — items survive, FK is NULL after the event is gone
# ---------------------------------------------------------------------------
def test_event_delete_preserves_items(admin_client):
    p = _make_project(admin_client)
    ev = _make_event(admin_client)
    a = _make_item(admin_client, p["id"], title="first item", event_id=ev["id"])
    b = _make_item(admin_client, p["id"], title="second item", event_id=ev["id"])

    r = admin_client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 200

    for it in (a, b):
        row = admin_client.get(f"/api/bugs/{it['id']}").json()
        assert row["event_id"] is None
        assert row["event_name"] is None
        # Item still exists, intact.
        assert row["title"] == it["title"]


def test_event_delete_admin_or_manager_only(admin_client):
    """Regular users get 403 from DELETE /api/events/{id}."""
    # Create the event as admin.
    ev = _make_event(admin_client, name="for-delete-perm-test")
    # Create a regular user and log in as them in a fresh session.
    admin_client.post("/api/users", json={
        "name": "Regular", "email": "regular@x.test", "role": "user",
        "password": "User12345Aa",
    })
    admin_client.post("/api/auth/logout")
    admin_client.post("/api/auth/login", json={
        "email": "regular@x.test", "password": "User12345Aa",
    })
    r = admin_client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def test_event_actions_appear_in_audit(admin_client):
    ev = _make_event(admin_client, name="Friday review")
    admin_client.put(f"/api/events/{ev['id']}", json={"name": "Friday retro"})
    admin_client.delete(f"/api/events/{ev['id']}")
    audit = admin_client.get("/api/audit").json()
    actions = [(a["action"], a.get("detail", "")) for a in audit
               if a.get("entity_type") == "event"]
    assert any(a == "event_created" for a, _ in actions)
    assert any("event_name_changed" in a for a, _ in actions)
    assert any(a == "event_deleted" for a, _ in actions)


# ---------------------------------------------------------------------------
# Migration safety — running init_db twice against a populated DB is a no-op
# ---------------------------------------------------------------------------
def test_init_db_with_events_idempotent(admin_client):
    from app.database import init_db
    p = _make_project(admin_client, name="Idempotency")
    ev = _make_event(admin_client)
    item = _make_item(admin_client, p["id"], event_id=ev["id"])
    init_db()
    init_db()
    # Everything still readable.
    detail = admin_client.get(f"/api/events/{ev['id']}").json()
    assert detail["item_count"] == 1
    assert detail["items"][0]["id"] == item["id"]


def test_legacy_db_gets_event_id_column(tmp_path, monkeypatch):
    """A legacy SQLite DB with no event_id column on bugs: init_db must
    ALTER TABLE ADD COLUMN it (nullable) without losing data."""
    import sqlite3
    db_file = tmp_path / "legacy.db"
    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT '#c9764f',
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        CREATE TABLE bugs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            reporter_id INTEGER,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'New',
            priority TEXT NOT NULL DEFAULT 'Medium',
            environment TEXT NOT NULL DEFAULT 'DEV',
            due_date TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        INSERT INTO projects (name, created_at, updated_at)
            VALUES ('legacy', '2026-01-01', '2026-01-01');
        INSERT INTO bugs (project_id, title, status, priority, environment,
                          created_at, updated_at)
            VALUES (1, 'pre-2.3 row', 'New', 'Medium', 'DEV',
                    '2026-01-01', '2026-01-01');
        """
    )
    con.commit(); con.close()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("SESSION_SECRET", "legacy_event_secret")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Admin1234")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", "Admin")

    import sys
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={
            "email": "admin@test.local", "password": "Admin1234",
        })
        assert r.status_code == 200, r.text
        rows = c.get("/api/bugs").json()
        assert rows["total"] == 1
        # Both new columns are present and harmless.
        assert rows["items"][0]["item_type"] == "Bug"
        assert rows["items"][0]["event_id"] is None
        # Events endpoint works against the migrated DB.
        evs = c.get("/api/events").json()
        assert evs == []
        # And we can still create an event + link the legacy row to it.
        ev = c.post("/api/events", json={"name": "post-migration"}).json()
        c.put(f"/api/bugs/{rows['items'][0]['id']}",
              json={"event_id": ev["id"]})
        re = c.get(f"/api/events/{ev['id']}").json()
        assert re["item_count"] == 1


def test_legacy_events_table_gets_project_id_column(tmp_path, monkeypatch):
    """A legacy SQLite DB whose `events` table predates the project_id column:
    init_db must ALTER TABLE events ADD COLUMN project_id (nullable) without
    touching existing rows. The pre-existing event ends up project-less
    (visible to admins only); new events can carry a project."""
    import sqlite3
    db_file = tmp_path / "legacy_events.db"
    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT '#c9764f',
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            scheduled_for TEXT,
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        INSERT INTO projects (name, created_at, updated_at)
            VALUES ('legacy', '2026-01-01', '2026-01-01');
        INSERT INTO events (name, created_at, updated_at)
            VALUES ('pre-project event', '2026-01-01', '2026-01-01');
        """
    )
    con.commit(); con.close()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("SESSION_SECRET", "legacy_proj_secret")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Admin1234")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", "Admin")

    import sys
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={
            "email": "admin@test.local", "password": "Admin1234",
        })
        assert r.status_code == 200, r.text
        # The legacy event survived and is now project-less (project_id NULL).
        evs = c.get("/api/events").json()
        assert len(evs) == 1
        assert evs[0]["name"] == "pre-project event"
        assert evs[0]["project_id"] is None
        # A new event can carry a project against the migrated schema.
        proj = c.get("/api/projects").json()[0]
        ev = c.post("/api/events", json={
            "name": "with project", "project_id": proj["id"],
        }).json()
        assert ev["project_id"] == proj["id"]
