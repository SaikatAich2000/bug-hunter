"""Basic sanity tests for backlog ranking and the sprint lifecycle
(create/start/complete/cancel), plus hierarchy assignment.
"""
from __future__ import annotations


def _setup_project_with_board(admin_client, name="Sprint Suite"):
    res = admin_client.post("/api/projects", json={"name": name, "description": "", "color": "#c9764f"})
    assert res.status_code == 201, res.text
    project = res.json()
    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert enable_res.status_code == 200, enable_res.text
    return project, enable_res.json()["board_id"]


def _create_story(admin_client, project_id, title):
    res = admin_client.post("/api/agile/work-items", json={
        "project_id": project_id, "title": title, "item_type": "story",
    })
    assert res.status_code == 201, res.text
    return res.json()


def test_backlog_lists_new_items_ranked(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Backlog Suite")
    a = _create_story(admin_client, project["id"], "Story A")
    b = _create_story(admin_client, project["id"], "Story B")

    res = admin_client.get(f"/api/agile/boards/{board_id}/backlog")
    assert res.status_code == 200
    ids = [i["id"] for i in res.json()]
    assert a["id"] in ids
    assert b["id"] in ids
    # Creation order preserved by rank.
    assert ids.index(a["id"]) < ids.index(b["id"])


def test_backlog_rank_reorder(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Rank Suite")
    a = _create_story(admin_client, project["id"], "Story A")
    b = _create_story(admin_client, project["id"], "Story B")

    # Move B before A.
    res = admin_client.post(f"/api/agile/boards/{board_id}/rank", json={
        "item_id": b["id"], "before_id": None, "after_id": a["id"],
    })
    assert res.status_code == 200, res.text

    backlog = admin_client.get(f"/api/agile/boards/{board_id}/backlog").json()
    ids = [i["id"] for i in backlog]
    assert ids.index(b["id"]) < ids.index(a["id"])


def test_sprint_full_lifecycle_start_complete(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Lifecycle Suite")
    item = _create_story(admin_client, project["id"], "Do the thing")

    sprint_res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Sprint 1", "goal": "Ship it",
    })
    assert sprint_res.status_code == 201, sprint_res.text
    sprint = sprint_res.json()
    assert sprint["state"] == "future"

    add_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})
    assert add_res.status_code == 200, add_res.text

    start_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]})
    assert start_res.status_code == 200, start_res.text
    started = start_res.json()
    assert started["state"] == "active"

    history_res = admin_client.get(f"/api/agile/sprints/{sprint['id']}/history")
    assert history_res.status_code == 200
    assert any(h["event_type"] == "committed" for h in history_res.json())

    complete_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/complete", json={
        "closing_note": "done for now", "version": started["version"],
        "default_destination": "backlog",
    })
    assert complete_res.status_code == 200, complete_res.text
    assert complete_res.json()["state"] == "closed"


def test_sprint_cannot_start_empty(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Empty Sprint Suite")
    sprint_res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Empty Sprint"})
    sprint = sprint_res.json()
    start_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]})
    assert start_res.status_code == 409


def test_sprint_cancel_returns_items_to_backlog(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Cancel Suite")
    item = _create_story(admin_client, project["id"], "Cancel me maybe")
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Sprint X"}).json()
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})

    cancel_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/cancel", json={
        "cancellation_note": "changed plans", "version": sprint["version"],
        "default_destination": "backlog",
    })
    assert cancel_res.status_code == 200, cancel_res.text
    assert cancel_res.json()["state"] == "cancelled"

    backlog = admin_client.get(f"/api/agile/boards/{board_id}/backlog").json()
    assert any(i["id"] == item["id"] for i in backlog)


def test_start_sprint_idempotency_key_replays(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Idempotent Sprint Suite")
    item = _create_story(admin_client, project["id"], "Idempotent item")
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Sprint Idem"}).json()
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})

    headers = {"Idempotency-Key": "test-key-123"}
    r1 = admin_client.post(
        f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]}, headers=headers,
    )
    assert r1.status_code == 200
    r2 = admin_client.post(
        f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]}, headers=headers,
    )
    assert r2.status_code == 200
    assert r1.json() == r2.json()


def test_hierarchy_subtask_requires_level2_parent(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Hierarchy Suite")
    story = _create_story(admin_client, project["id"], "Parent story")

    sub_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Sub work", "item_type": "sub-task",
        "parent_id": story["id"],
    })
    assert sub_res.status_code == 201, sub_res.text
    assert sub_res.json()["id"] is not None

    bad_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Orphan subtask", "item_type": "sub-task",
    })
    assert bad_res.status_code == 422


def test_legacy_bugs_endpoint_rejects_agile_types(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Legacy Guard Suite")
    res = admin_client.post("/api/bugs", json={
        "project_id": project["id"], "title": "Should be rejected", "item_type": "Epic",
    })
    assert res.status_code == 422


def test_create_sprint_creates_exactly_one_sprint(admin_client):
    """POST /sprints creates one sprint and returns it; the removed bulk-generation fields
    are ignored rather than fanning out into a series."""
    project, board_id = _setup_project_with_board(admin_client, "Single Suite")
    res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Sprint One", "goal": "Single", "start_date": "2026-03-01", "end_date": "2026-03-07",
        "generate": True,
        "periods": [{"start_date": "2026-03-08", "end_date": "2026-03-14"}] * 50,
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "Sprint One"
    assert (body["start_date"], body["end_date"]) == ("2026-03-01", "2026-03-07")
    assert "created" not in body
    sprints = admin_client.get(f"/api/agile/sprints?board_id={board_id}").json()
    assert [s["name"] for s in sprints] == ["Sprint One"]


def test_epic_update_includes_target_date(admin_client):
    """Epic update must persist target_date and start_date so the edit form
    can round-trip values back to the API."""
    project, board_id = _setup_project_with_board(admin_client, "Epic Date Suite")
    epic_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Epic With Dates", "item_type": "epic",
    })
    assert epic_res.status_code == 201, epic_res.text
    epic = epic_res.json()

    update_res = admin_client.put(f"/api/agile/epics/{epic['id']}", json={
        "title": "Epic Updated", "description": "Updated",
        "start_date": "2026-04-01", "target_date": "2026-04-30",
        "version": epic["version"],
    })
    assert update_res.status_code == 200, update_res.text
    updated = update_res.json()
    assert updated["title"] == "Epic Updated"
    assert updated["start_date"] == "2026-04-01"
    assert updated["target_date"] == "2026-04-30"
    assert updated["display_id"] == f"EPIC-{epic['id']}"

    backwards = admin_client.put(f"/api/agile/epics/{epic['id']}", json={
        "start_date": "2026-05-01", "target_date": "2026-04-01", "version": updated["version"],
    })
    assert backwards.status_code == 422, backwards.text


def test_epics_are_never_planned_into_sprints(admin_client):
    """Sprints plan issues, not Epics: an Epic cannot be created in, added to,
    or updated into a sprint, whatever the route."""
    project, board_id = _setup_project_with_board(admin_client, "Epic Sprint Suite")
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Sprint A"}).json()

    created = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Epic in sprint", "item_type": "epic",
        "sprint_id": sprint["id"],
    })
    assert created.status_code == 422, created.text

    epic = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Plain epic", "item_type": "epic",
    }).json()
    added = admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [epic["id"]]})
    assert added.status_code == 409, added.text
    assert "Epics are not planned" in added.json()["detail"]

    # A sprint_id sent to the epic update is not part of the contract and is ignored.
    updated = admin_client.put(f"/api/agile/epics/{epic['id']}", json={
        "sprint_id": sprint["id"], "version": 1,
    })
    assert updated.status_code == 200, updated.text
    item = admin_client.get(f"/api/bugs/{epic['id']}").json()
    assert item["sprint_id"] is None


def test_retired_hierarchy_transition_routes_are_gone(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Retired Transition Suite")
    for path in ("/api/agile/collections/1/transition", "/api/agile/features/1/transition"):
        res = admin_client.post(path, json={"to_status": "In Progress", "version": 1})
        assert res.status_code in (404, 405), res.text


def test_epic_transition_via_work_items_endpoint(admin_client):
    """Epic (stored in Bug table) must transition via the work-items endpoint."""
    project, board_id = _setup_project_with_board(admin_client, "Epic Transition Suite")
    epic_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Drag Epic", "item_type": "epic",
    })
    assert epic_res.status_code == 201, epic_res.text
    epic = epic_res.json()

    # Epic is a Bug row, so it uses the work-items endpoint
    res = admin_client.post(f"/api/agile/work-items/{epic['id']}/transition", json={
        "to_status": "In Progress", "version": epic["version"],
    })
    assert res.status_code == 200, res.text
    updated = res.json()
    assert updated["id"] == epic["id"]


# Focused regression tests for the four defects:
#   1. Single Task Type   2. Universal Status Support
#   3. False Concurrency  4. Unrestricted Status Movement


def _create_item(client, project_id, title, item_type, **extra):
    """Create a work item. Agile hierarchy types (Epic/Story/Sub-task) go
    through /api/agile/work-items; legacy types (Bug/Requirement/Task) go
    through /api/bugs. Returns the created item dict."""
    # Normalize to canonical case for the agile endpoint (which validates
    # against AGILE_ITEM_TYPES exactly); the bugs endpoint is case-insensitive.
    canonical = {"epic": "Epic", "story": "Story", "sub-task": "Sub-task"}.get(
        item_type.lower(), item_type,
    )
    payload = {"project_id": project_id, "title": title, "item_type": canonical}
    payload.update(extra)
    if canonical in ("Epic", "Story", "Sub-task"):
        res = client.post("/api/agile/work-items", json=payload)
    else:
        res = client.post("/api/bugs", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


def test_task_and_sub_task_each_use_their_own_workflow(admin_client):
    """Task (a standard issue) and Sub-task are different types: each resolves
    to its own workflow statuses, and a Task moves on the board."""
    from app.agile.workflow import canonical_work_item_type, get_status_by_value
    from app.database import SessionLocal

    assert canonical_work_item_type("task") == "Task"
    assert canonical_work_item_type("Sub-task") == "Sub-task"
    with SessionLocal() as db:
        task_row = get_status_by_value(db, "Task", "In Progress")
        sub_row = get_status_by_value(db, "Sub-task", "In Progress")
        assert task_row is not None
        assert sub_row is not None
        assert (task_row.work_item_type, sub_row.work_item_type) == ("Task", "Sub-task")
    project, board_id = _setup_project_with_board(admin_client, "Task Workflow Suite")
    task = _create_item(admin_client, project["id"], "A task", "Task")
    res = admin_client.post(f"/api/agile/work-items/{task['id']}/transition", json={
        "to_status": "In Progress", "version": task["version"],
    })
    assert res.status_code == 200, res.text
    assert res.json()["item_type"] == "Task"


def test_single_task_type_status_vocabularies_match(admin_client):
    """Defect 1: 'Task' and 'Sub-task' must share one canonical status set."""
    from app.schemas import STATUSES_BY_TYPE
    assert STATUSES_BY_TYPE["Task"] == STATUSES_BY_TYPE["Sub-task"]


def test_universal_status_epic_can_reach_testing(admin_client):
    """Defect 2: Epic must accept 'Testing' (previously raised TransitionError)."""
    project, board_id = _setup_project_with_board(admin_client, "Epic Testing Suite")
    epic = _create_item(admin_client, project["id"], "Epic Under Test", "Epic")

    res = admin_client.post(f"/api/agile/work-items/{epic['id']}/transition", json={
        "to_status": "Testing", "version": epic["version"],
    })
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "Testing"


def test_universal_status_matrix(admin_client):
    """Defect 2: every configured status must be reachable for every item type."""
    from app.schemas import CANONICAL_STATUSES, STATUSES_BY_TYPE
    for item_type, statuses in STATUSES_BY_TYPE.items():
        for status in CANONICAL_STATUSES:
            assert status in statuses, f"{status} missing for {item_type}"


def test_unrestricted_status_movement_requirement_out_of_done(admin_client):
    """Defect 4: a Requirement in Done must be movable to every other status."""
    project, board_id = _setup_project_with_board(admin_client, "Requirement Move Suite")
    req = _create_item(admin_client, project["id"], "Movable Requirement", "requirement")

    # Move Requirement to Done first.
    res = admin_client.post(f"/api/agile/work-items/{req['id']}/transition", json={
        "to_status": "Done", "version": req["version"],
    })
    assert res.status_code == 200, res.text
    done_version = res.json()["version"]

    # Now move it out of Done to several other statuses.
    for target in ("In Progress", "Testing", "New", "Blocked"):
        res = admin_client.post(f"/api/agile/work-items/{req['id']}/transition", json={
            "to_status": target, "version": done_version,
        })
        assert res.status_code == 200, f"Done -> {target} failed: {res.text}"
        done_version = res.json()["version"]


def test_unrestricted_status_movement_all_types_leave_done(admin_client):
    """Defect 4: every item type must be able to move out of Done.

    All work-item types are stored in the bugs table, so they share the
    work-items transition endpoint regardless of which create path was used."""
    project, board_id = _setup_project_with_board(admin_client, "All Types Move Suite")
    item_types = ["story", "bug", "epic", "requirement"]
    for item_type in item_types:
        item = _create_item(admin_client, project["id"], f"Move {item_type}", item_type)
        res = admin_client.post(f"/api/agile/work-items/{item['id']}/transition", json={
            "to_status": "Done", "version": item["version"],
        })
        assert res.status_code == 200, f"{item_type} -> Done failed: {res.text}"
        done_version = res.json()["version"]

        res = admin_client.post(f"/api/agile/work-items/{item['id']}/transition", json={
            "to_status": "In Progress", "version": done_version,
        })
        assert res.status_code == 200, f"{item_type} Done -> In Progress failed: {res.text}"


def test_concurrency_rapid_repeated_moves(admin_client):
    """Defect 3: rapid sequential moves must not raise a false 409."""
    project, board_id = _setup_project_with_board(admin_client, "Rapid Move Suite")
    story = _create_item(admin_client, project["id"], "Rapid Story", "story")
    version = story["version"]

    # Issue three transitions back-to-back using the version returned by each
    # response. This simulates fast drag-and-drop without waiting for a full
    # page refresh between moves.
    path = ["In Progress", "Testing", "Done"]
    for target in path:
        res = admin_client.post(f"/api/agile/work-items/{story['id']}/transition", json={
            "to_status": target, "version": version,
        })
        assert res.status_code == 200, f"Move to {target} failed: {res.text}"
        version = res.json()["version"]

    # Final state must be Done and persisted.
    final = admin_client.get(f"/api/bugs/{story['id']}").json()
    assert final["status"] == "Done"


def test_settings_self_heals_agile_enabled_without_board(admin_client):
    """Regression: a project flagged agile_enabled=True with no default board
    (e.g. after a database reset that dropped board rows) made the Sprint Board
    and Sprint Items tabs render nothing at all, the settings gate
    (`agile_enabled && board_id`) failed and the panels returned null. The
    settings endpoint must report a consistent, recoverable state instead.
    """
    from sqlalchemy import delete, select

    from app.database import SessionLocal
    from app.models import Board, BoardColumn, BoardColumnStatus

    project, board_id = _setup_project_with_board(admin_client, "Heal Suite")

    # Simulate the corrupted state directly: board rows gone, flag still set.
    with SessionLocal() as db:
        column_ids = select(BoardColumn.id).where(BoardColumn.board_id == board_id)
        db.execute(delete(BoardColumnStatus).where(BoardColumnStatus.board_column_id.in_(column_ids)))
        db.execute(delete(BoardColumn).where(BoardColumn.board_id == board_id))
        db.execute(delete(Board).where(Board.id == board_id))
        db.commit()

    res = admin_client.get(f"/api/agile/projects/{project['id']}/settings")
    assert res.status_code == 200, res.text
    body = res.json()
    # The impossible combination must be healed to a recoverable one, so the
    # UI shows the "Agile is not enabled / Enable Agile" panel instead of blank.
    assert body["agile_enabled"] is False
    assert body["board_id"] is None

    # And the project can be re-enabled cleanly afterwards.
    enable = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert enable.status_code == 200, enable.text
    assert enable.json()["agile_enabled"] is True
    assert enable.json()["board_id"] is not None



# lifecycle guards: stale versions, bad edits, idempotent replays

def _sprint(admin_client, board_id, name, **extra):
    res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": name, **extra})
    assert res.status_code == 201, res.text
    return res.json()


def _put_in(admin_client, sprint_id, *item_ids):
    res = admin_client.post(f"/api/agile/sprints/{sprint_id}/items", json={"item_ids": list(item_ids)})
    assert res.status_code == 200, res.text


def test_every_lifecycle_route_rejects_a_stale_version(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Stale Versions")
    item = _create_story(admin_client, project["id"], "Versioned")
    sprint = _sprint(admin_client, board_id, "Stale 1")
    _put_in(admin_client, sprint["id"], item["id"])
    stale = sprint["version"] + 7

    base = f"/api/agile/sprints/{sprint['id']}"
    assert admin_client.put(base, json={"goal": "x", "version": stale}).status_code == 409
    assert admin_client.post(f"{base}/start", json={"version": stale}).status_code == 409
    started = admin_client.post(f"{base}/start", json={"version": sprint["version"]}).json()
    assert admin_client.post(f"{base}/complete", json={"version": stale}).status_code == 409
    cancel = admin_client.post(f"{base}/cancel", json={"cancellation_note": "n", "version": stale})
    assert cancel.status_code == 409
    assert admin_client.get(base).json()["version"] == started["version"]


def test_sprint_edit_rules(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Edit Rules")
    item = _create_story(admin_client, project["id"], "Editable")
    sprint = _sprint(admin_client, board_id, "Edit 1", start_date="2026-03-01", end_date="2026-03-14")
    other = _sprint(admin_client, board_id, "Edit 2")
    base = f"/api/agile/sprints/{sprint['id']}"

    swapped = admin_client.put(base, json={"start_date": "2026-04-01", "version": sprint["version"]})
    assert swapped.status_code == 409
    assert "end date" in swapped.json()["detail"]
    edited = admin_client.put(base, json={"goal": "Ship", "cadence": "weekly", "version": sprint["version"]})
    assert edited.status_code == 200, edited.text
    assert (edited.json()["goal"], edited.json()["cadence"]) == ("Ship", "weekly")
    cleared = admin_client.put(base, json={"start_date": None, "end_date": None,
                                           "version": edited.json()["version"]})
    assert (cleared.json()["start_date"], cleared.json()["end_date"]) == (None, None)
    assert cleared.json()["cadence"] == "weekly"
    clash = admin_client.put(base, json={"name": other["name"].lower(), "version": cleared.json()["version"]})
    assert clash.status_code == 409

    _put_in(admin_client, sprint["id"], item["id"])
    started = admin_client.post(f"{base}/start", json={
        "version": cleared.json()["version"], "start_date": "2026-05-01", "end_date": "2026-05-14",
    }).json()
    keep_dates = admin_client.put(base, json={"end_date": None, "version": started["version"]})
    assert keep_dates.status_code == 409
    assert "keeps its start and end dates" in keep_dates.json()["detail"]

    closed = admin_client.post(f"{base}/complete", json={"version": started["version"]}).json()
    assert admin_client.put(base, json={"goal": "late", "version": closed["version"]}).status_code == 409


def test_starting_with_a_taken_name_conflicts(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Start Names")
    item = _create_story(admin_client, project["id"], "Named")
    taken = _sprint(admin_client, board_id, "Taken name")
    sprint = _sprint(admin_client, board_id, "Fresh name")
    _put_in(admin_client, sprint["id"], item["id"])
    res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/start",
                            json={"name": taken["name"], "version": sprint["version"]})
    assert res.status_code == 409


def test_state_rules_for_start_complete_cancel_and_remove(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "State Rules")
    item = _create_story(admin_client, project["id"], "Stateful")
    sprint = _sprint(admin_client, board_id, "State 1")
    base = f"/api/agile/sprints/{sprint['id']}"
    _put_in(admin_client, sprint["id"], item["id"])

    assert admin_client.post(f"{base}/complete", json={"version": sprint["version"]}).status_code == 409
    assert admin_client.delete(f"{base}/items/999999").status_code == 404
    started = admin_client.post(f"{base}/start", json={"version": sprint["version"]}).json()
    assert admin_client.post(f"{base}/start", json={"version": started["version"]}).status_code == 409

    # A finished issue stays in the closed sprint.
    current = admin_client.get(f"/api/bugs/{item['id']}").json()
    done = admin_client.post(f"/api/agile/work-items/{item['id']}/transition",
                             json={"to_status": "Done", "version": current["version"]})
    assert done.status_code == 200, done.text
    closed = admin_client.post(f"{base}/complete", json={"version": started["version"]}).json()
    assert admin_client.delete(f"{base}/items/{item['id']}").status_code == 409
    again = admin_client.post(f"{base}/cancel", json={"cancellation_note": "n", "version": closed["version"]})
    assert again.status_code == 409
    assert admin_client.post(f"{base}/items", json={"item_ids": [item["id"]]}).status_code == 409


def test_completion_and_cancellation_validate_destinations(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Destinations")
    item = _create_story(admin_client, project["id"], "Unfinished")
    sprint = _sprint(admin_client, board_id, "Dest 1")
    base = f"/api/agile/sprints/{sprint['id']}"
    _put_in(admin_client, sprint["id"], item["id"])
    started = admin_client.post(f"{base}/start", json={"version": sprint["version"]}).json()

    stray = {"item_id": 999999, "destination": "backlog"}
    res = admin_client.post(f"{base}/complete", json={"version": started["version"], "dispositions": [stray]})
    assert res.status_code == 409
    to_missing = admin_client.post(f"{base}/complete", json={
        "version": started["version"], "default_destination": "sprint", "default_target_sprint_id": 999999,
    })
    assert to_missing.status_code == 409
    res = admin_client.post(f"{base}/cancel", json={
        "cancellation_note": "n", "version": started["version"], "dispositions": [stray],
    })
    assert res.status_code == 409
    # Nothing above changed the sprint.
    assert admin_client.get(base).json()["state"] == "active"


def test_unfinished_issues_can_open_a_new_sprint_with_a_default_name(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "New Sprint Names")
    item = _create_story(admin_client, project["id"], "Carried")
    sprint = _sprint(admin_client, board_id, "Names 1")
    base = f"/api/agile/sprints/{sprint['id']}"
    _put_in(admin_client, sprint["id"], item["id"])
    started = admin_client.post(f"{base}/start", json={"version": sprint["version"]}).json()

    body = {
        "version": started["version"], "default_destination": "new_sprint",
        "dispositions": [{"item_id": item["id"], "destination": "new_sprint"}],
    }
    res = admin_client.post(f"{base}/complete", json=body, headers={"Idempotency-Key": "complete-1"})
    assert res.status_code == 200, res.text
    sprints = admin_client.get(f"/api/agile/sprints?board_id={board_id}").json()
    assert [s["name"] for s in sprints if s["state"] == "future"] == ["Sprint 2"]
    replay = admin_client.post(f"{base}/complete", json=body, headers={"Idempotency-Key": "complete-1"})
    assert replay.json() == res.json()


def test_cancelling_an_active_sprint_logs_removals_and_replays(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Cancel Active")
    item = _create_story(admin_client, project["id"], "Pulled")
    sprint = _sprint(admin_client, board_id, "Cancel 1")
    target = _sprint(admin_client, board_id, "Cancel target")
    base = f"/api/agile/sprints/{sprint['id']}"
    _put_in(admin_client, sprint["id"], item["id"])
    started = admin_client.post(f"{base}/start", json={"version": sprint["version"]}).json()

    body = {"cancellation_note": "descoped", "version": started["version"],
            "dispositions": [{"item_id": item["id"], "destination": "sprint", "target_sprint_id": target["id"]}]}
    res = admin_client.post(f"{base}/cancel", json=body, headers={"Idempotency-Key": "cancel-1"})
    assert res.status_code == 200, res.text
    assert admin_client.post(f"{base}/cancel", json=body, headers={"Idempotency-Key": "cancel-1"}).json() == res.json()
    events = [h["event_type"] for h in admin_client.get(f"{base}/history").json()]
    assert "removed" in events
    moved = admin_client.get(f"/api/agile/sprints/{target['id']}").json()
    assert moved["item_count"] == 1


def test_adding_issues_to_a_sprint_enforces_planning_rules(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Planning Rules")
    other_project, other_board = _setup_project_with_board(admin_client, "Other Planning")
    story = _create_story(admin_client, project["id"], "Plannable")
    epic = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Epic", "item_type": "epic"}).json()
    foreign = _create_story(admin_client, other_project["id"], "Foreign")
    sprint = _sprint(admin_client, board_id, "Plan 1")
    foreign_sprint = _sprint(admin_client, other_board, "Foreign plan")
    url = f"/api/agile/sprints/{sprint['id']}/items"

    assert admin_client.post(url, json={"item_ids": [epic["id"]]}).status_code == 409
    assert admin_client.post(url, json={"item_ids": [foreign["id"]]}).status_code == 409
    assert admin_client.post(url, json={"item_ids": [story["id"]], "before_id": story["id"]}).status_code == 409
    moved = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Straight in", "item_type": "story", "sprint_id": sprint["id"]})
    assert moved.status_code == 201, moved.text
    for bad in (999999, foreign_sprint["id"]):
        res = admin_client.post("/api/agile/work-items", json={
            "project_id": project["id"], "title": "Nowhere", "item_type": "story", "sprint_id": bad})
        assert res.status_code == 422
    for blocked in ("epic", "sub-task"):
        res = admin_client.post("/api/agile/work-items", json={
            "project_id": project["id"], "title": "Blocked", "item_type": blocked, "sprint_id": sprint["id"]})
        assert res.status_code == 422


def test_hierarchy_route_reports_missing_and_stale_items(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Hierarchy Errors")
    story = _create_story(admin_client, project["id"], "Child")
    assert admin_client.put("/api/agile/work-items/999999/hierarchy",
                            json={"epic_id": None, "version": 1}).status_code == 404
    stale = admin_client.put(f"/api/agile/work-items/{story['id']}/hierarchy",
                             json={"epic_id": None, "version": story["version"] + 4})
    assert stale.status_code == 409
