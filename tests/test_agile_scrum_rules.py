"""Scrum rules for sprints, the backlog, the board and the
issue hierarchy. Each test states the rule it pins."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.auth import hash_password
from app.models import ROLE_ADMIN, AcceptanceCriterion, Bug, Project, User
from app.schemas import WorkItemTaskFlagsIn
from app.services.agile_items import apply_task_flag_updates as _apply_task_flag_updates
from app.services.agile_items import validate_story_ready_requirements as _validate_story_ready_requirements

# --- helpers ---------------------------------------------------------------

def _project(client, name):
    project = client.post("/api/projects", json={"name": name, "color": "#c9764f"}).json()
    board_id = client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}}).json()["board_id"]
    return project, board_id


def _issue(client, project_id, title, item_type="Story", **extra):
    res = client.post("/api/agile/work-items", json={
        "project_id": project_id, "title": title, "item_type": item_type, **extra,
    })
    assert res.status_code == 201, res.text
    return res.json()


def _bug(client, project_id, title, item_type="Bug", **extra):
    res = client.post("/api/bugs", json={"project_id": project_id, "title": title, "item_type": item_type, **extra})
    assert res.status_code == 201, res.text
    return res.json()


def _sprint(client, board_id, name="Sprint 1", **extra):
    res = client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": name, **extra})
    assert res.status_code == 201, res.text
    return res.json()


def _fresh(client, sprint_id):
    return client.get(f"/api/agile/sprints/{sprint_id}").json()


def _row(item_id):
    from app.database import SessionLocal
    from app.models import Bug

    with SessionLocal() as db:
        row = db.get(Bug, item_id)
        db.expunge(row)
        return row


def _set_status(client, item_id, status):
    item = client.get(f"/api/bugs/{item_id}").json()
    res = client.post(f"/api/agile/work-items/{item_id}/transition",
                      json={"to_status": status, "version": item["version"]})
    assert res.status_code == 200, res.text


def _add(client, sprint_id, *item_ids):
    res = client.post(f"/api/agile/sprints/{sprint_id}/items", json={"item_ids": list(item_ids)})
    assert res.status_code == 200, res.text


def _start(client, sprint_id, **extra):
    sprint = _fresh(client, sprint_id)
    res = client.post(f"/api/agile/sprints/{sprint_id}/start", json={"version": sprint["version"], **extra})
    assert res.status_code == 200, res.text
    return res.json()


def _complete(client, sprint_id, **extra):
    sprint = _fresh(client, sprint_id)
    return client.post(f"/api/agile/sprints/{sprint_id}/complete", json={"version": sprint["version"], **extra})


def _planning(client, board_id):
    res = client.get(f"/api/agile/boards/{board_id}/planning")
    assert res.status_code == 200, res.text
    return res.json()


def _backlog_ids(client, board_id):
    return [i["id"] for i in _planning(client, board_id)["backlog"]]


# --- starting a sprint ----------------------------------------------------------

def test_start_sprint_sets_dates_and_the_exact_start_instant(admin_client):
    """The Start Sprint dialog: name, goal and dates may be set; without an
    end date the sprint runs two weeks."""
    project, board_id = _project(admin_client, "Start Suite")
    story = _issue(admin_client, project["id"], "Work")
    sprint = _sprint(admin_client, board_id)
    _add(admin_client, sprint["id"], story["id"])
    started = _start(admin_client, sprint["id"], name="Launch sprint", goal="Ship it")
    assert started["state"] == "active"
    assert started["name"] == "Launch sprint"
    assert started["goal"] == "Ship it"
    assert started["started_at"] is not None
    start = date.fromisoformat(started["start_date"])
    assert date.fromisoformat(started["end_date"]) == start + timedelta(days=13)


def test_empty_or_second_sprint_cannot_start(admin_client):
    project, board_id = _project(admin_client, "Start Rules Suite")
    empty = _sprint(admin_client, board_id, "Empty")
    res = admin_client.post(f"/api/agile/sprints/{empty['id']}/start", json={"version": empty["version"]})
    assert res.status_code == 409
    assert "without issues" in res.json()["detail"]
    first, second = _sprint(admin_client, board_id, "First"), _sprint(admin_client, board_id, "Second")
    _add(admin_client, first["id"], _issue(admin_client, project["id"], "Issue A")["id"])
    _add(admin_client, second["id"], _issue(admin_client, project["id"], "Issue B")["id"])
    _start(admin_client, first["id"])
    res = admin_client.post(f"/api/agile/sprints/{second['id']}/start", json={"version": second["version"]})
    assert res.status_code == 409
    assert "already active" in res.json()["detail"]


def test_start_rejects_an_end_before_the_start(admin_client):
    project, board_id = _project(admin_client, "Start Dates Suite")
    sprint = _sprint(admin_client, board_id)
    _add(admin_client, sprint["id"], _issue(admin_client, project["id"], "Issue A")["id"])
    res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/start", json={
        "version": sprint["version"], "start_date": "2026-05-10", "end_date": "2026-05-01",
    })
    assert res.status_code == 422


# --- completing a sprint --------------------------------------------------------

def test_completed_issues_stay_in_the_closed_sprint(admin_client):
    """Finished issues stay in the sprint they were finished in (the
    sprint report and velocity read them there); unfinished ones move."""
    project, board_id = _project(admin_client, "Complete Suite")
    done = _issue(admin_client, project["id"], "Finished", story_points=3)
    open_ = _issue(admin_client, project["id"], "Unfinished", story_points=5)
    sprint = _sprint(admin_client, board_id)
    _add(admin_client, sprint["id"], done["id"], open_["id"])
    _start(admin_client, sprint["id"])
    _set_status(admin_client, done["id"], "Done")
    res = _complete(admin_client, sprint["id"])
    assert res.status_code == 200, res.text
    assert _row(done["id"]).sprint_id == sprint["id"]
    assert _row(open_["id"]).sprint_id is None
    assert open_["id"] in _backlog_ids(admin_client, board_id)
    assert done["id"] not in _backlog_ids(admin_client, board_id)
    # A finished issue in a closed sprint cannot be re-planned from there.
    future = _sprint(admin_client, board_id, "Next")
    res = admin_client.post(f"/api/agile/sprints/{future['id']}/items", json={"item_ids": [done["id"]]})
    assert res.status_code == 409
    assert "closed sprint" in res.json()["detail"]


def test_a_parent_with_open_subtasks_is_not_complete(admin_client):
    """An issue counts as complete only when it and all its Sub-tasks are in the
    board's right-most column; otherwise it moves on, Sub-tasks included."""
    project, board_id = _project(admin_client, "Subtask Completion Suite")
    parent = _issue(admin_client, project["id"], "Parent", story_points=8)
    sub_done = _issue(admin_client, project["id"], "Done part", "Sub-task", parent_id=parent["id"])
    sub_open = _issue(admin_client, project["id"], "Open part", "Sub-task", parent_id=parent["id"])
    sprint = _sprint(admin_client, board_id)
    _add(admin_client, sprint["id"], parent["id"])
    _start(admin_client, sprint["id"])
    _set_status(admin_client, parent["id"], "Done")
    _set_status(admin_client, sub_done["id"], "Done")
    res = _complete(admin_client, sprint["id"], default_destination="new_sprint", new_sprint_name="Follow-up")
    assert res.status_code == 200, res.text
    moved_to = _row(parent["id"]).sprint_id
    assert moved_to not in (None, sprint["id"])
    assert _fresh(admin_client, moved_to)["name"] == "Follow-up"
    assert _row(sub_done["id"]).sprint_id == moved_to
    assert _row(sub_open["id"]).sprint_id == moved_to
    report = admin_client.get(f"/api/agile/reports/sprint/{sprint['id']}").json()
    assert [r["id"] for r in report["not_completed"]] == [parent["id"]]
    assert report["completed"] == []


def test_unfinished_issues_can_go_to_a_future_sprint(admin_client):
    project, board_id = _project(admin_client, "Carry Over Suite")
    a, b = _issue(admin_client, project["id"], "Issue A"), _issue(admin_client, project["id"], "Issue B")
    sprint, future = _sprint(admin_client, board_id, "Now"), _sprint(admin_client, board_id, "Later")
    _add(admin_client, sprint["id"], a["id"], b["id"])
    _start(admin_client, sprint["id"])
    res = _complete(admin_client, sprint["id"], default_destination="sprint", default_target_sprint_id=future["id"],
                    dispositions=[{"item_id": b["id"], "destination": "backlog"}])
    assert res.status_code == 200, res.text
    assert _row(a["id"]).sprint_id == future["id"]
    assert _row(b["id"]).sprint_id is None
    # Carried-over work lands at the top of the target list.
    planning = _planning(admin_client, board_id)
    assert planning["backlog"][0]["id"] == b["id"]


def test_complete_rejects_bad_destinations_without_changing_anything(admin_client):
    project, board_id = _project(admin_client, "Bad Destination Suite")
    a = _issue(admin_client, project["id"], "Issue A")
    sprint = _sprint(admin_client, board_id)
    _add(admin_client, sprint["id"], a["id"])
    _start(admin_client, sprint["id"])
    res = _complete(admin_client, sprint["id"], default_destination="sprint", default_target_sprint_id=sprint["id"])
    assert res.status_code == 409
    assert _fresh(admin_client, sprint["id"])["state"] == "active"
    assert _row(a["id"]).sprint_id == sprint["id"]
    res = _complete(admin_client, sprint["id"], default_destination="sprint")
    assert res.status_code == 422  # a target sprint is required


def test_carry_over_is_tagged_so_reports_do_not_count_it_as_removal(admin_client):
    from app.database import SessionLocal
    from app.models import WorkItemChange

    project, board_id = _project(admin_client, "Tag Suite")
    a = _issue(admin_client, project["id"], "Issue A", story_points=2)
    sprint = _sprint(admin_client, board_id)
    _add(admin_client, sprint["id"], a["id"])
    _start(admin_client, sprint["id"])
    assert _complete(admin_client, sprint["id"]).status_code == 200
    with SessionLocal() as db:
        moves = db.query(WorkItemChange).filter(
            WorkItemChange.work_item_id == a["id"], WorkItemChange.field == "sprint",
        ).order_by(WorkItemChange.id).all()
        assert moves[-1].reason == "sprint_completed"
    report = admin_client.get(f"/api/agile/reports/sprint/{sprint['id']}").json()
    assert report["removed"] == []
    assert [r["id"] for r in report["not_completed"]] == [a["id"]]


# --- the backlog ----------------------------------------------------------------

def test_backlog_holds_open_standard_issues_only(admin_client):
    """The backlog: issues in no open sprint and not in the Done column;
    never Epics and never Sub-tasks (they sit under their parent)."""
    project, board_id = _project(admin_client, "Backlog Suite")
    story = _issue(admin_client, project["id"], "Open story")
    task = _bug(admin_client, project["id"], "Open task", "Task")
    done = _bug(admin_client, project["id"], "Closed bug", status="Closed")
    epic = _issue(admin_client, project["id"], "Epic", "Epic")
    sub = _issue(admin_client, project["id"], "Sub", "Sub-task", parent_id=story["id"])
    ids = _backlog_ids(admin_client, board_id)
    assert story["id"] in ids
    assert task["id"] in ids
    for hidden in (done["id"], epic["id"], sub["id"]):
        assert hidden not in ids
    row = next(i for i in _planning(admin_client, board_id)["backlog"] if i["id"] == story["id"])
    assert (row["subtask_count"], row["subtasks_done"]) == (1, 0)


def test_drag_and_drop_positions_issues_between_neighbours(admin_client):
    project, board_id = _project(admin_client, "Rank Suite")
    a, b, c = (_issue(admin_client, project["id"], f"Issue {n}") for n in ("A", "B", "C"))
    assert _backlog_ids(admin_client, board_id) == [a["id"], b["id"], c["id"]]
    # C dropped between A and B.
    res = admin_client.post(f"/api/agile/boards/{board_id}/move", json={
        "item_ids": [c["id"]], "sprint_id": None, "before_id": a["id"], "after_id": b["id"],
    })
    assert res.status_code == 200, res.text
    assert _backlog_ids(admin_client, board_id) == [a["id"], c["id"], b["id"]]
    # A and B dragged together into a sprint keep their order.
    sprint = _sprint(admin_client, board_id)
    res = admin_client.post(f"/api/agile/boards/{board_id}/move", json={
        "item_ids": [a["id"], b["id"]], "sprint_id": sprint["id"],
    })
    assert res.status_code == 200, res.text
    planning = _planning(admin_client, board_id)
    assert [i["id"] for i in planning["sprints"][0]["issues"]] == [a["id"], b["id"]]
    # Back to the top of the backlog, above C.
    res = admin_client.post(f"/api/agile/boards/{board_id}/move", json={
        "item_ids": [b["id"]], "sprint_id": None, "after_id": c["id"],
    })
    assert res.status_code == 200, res.text
    assert _backlog_ids(admin_client, board_id) == [b["id"], c["id"]]


def test_moves_refuse_subtasks_epics_and_foreign_neighbours(admin_client):
    project, board_id = _project(admin_client, "Move Rules Suite")
    other, other_board = _project(admin_client, "Other Move Suite")
    story = _issue(admin_client, project["id"], "Story")
    sub = _issue(admin_client, project["id"], "Sub", "Sub-task", parent_id=story["id"])
    epic = _issue(admin_client, project["id"], "Epic", "Epic")
    foreign = _issue(admin_client, other["id"], "Foreign")
    for item_id, message in ((sub["id"], "moves with its parent"), (epic["id"], "Epics are not planned")):
        res = admin_client.post(f"/api/agile/boards/{board_id}/move", json={"item_ids": [item_id]})
        assert res.status_code == 409
        assert message in res.json()["detail"]
    res = admin_client.post(f"/api/agile/boards/{board_id}/move", json={
        "item_ids": [story["id"]], "before_id": foreign["id"],
    })
    assert res.status_code == 409
    res = admin_client.post(f"/api/agile/boards/{board_id}/move", json={"item_ids": [foreign["id"]]})
    assert res.status_code == 409
    del other_board


def test_regular_users_cannot_replan(admin_client):
    """Ranking and sprint planning need the manager's backlog permissions."""
    project, board_id = _project(admin_client, "Permission Suite")
    story = _issue(admin_client, project["id"], "Story")
    res = admin_client.post("/api/users", json={
        "name": "Member", "email": "member@test.local", "role": "user",
        "password": "Member12345", "project_ids": [project["id"]],
    })
    assert res.status_code == 201, res.text
    admin_client.post("/api/auth/logout")
    assert admin_client.post("/api/auth/login", json={
        "email": "member@test.local", "password": "Member12345",
    }).status_code == 200
    assert admin_client.get(f"/api/agile/boards/{board_id}/planning").status_code == 200
    res = admin_client.post(f"/api/agile/boards/{board_id}/move", json={"item_ids": [story["id"]]})
    assert res.status_code == 403


# --- board configuration -----------------------------------------------------------

def _columns(client, board_id):
    board = client.get(f"/api/agile/boards/{board_id}").json()
    return board, [
        {"id": c["id"], "name": c["name"], "category": c["category"], "statuses": c["statuses"],
         "wip_limit": c["wip_limit"], "min_cards": c["min_cards"], "wip_enforcement": c["wip_enforcement"]}
        for c in board["columns"]
    ]


def test_board_columns_update_in_place_and_validate(admin_client):
    project, board_id = _project(admin_client, "Columns Suite")
    board, cols = _columns(admin_client, board_id)
    ids_before = [c["id"] for c in cols]
    cols[0]["name"] = "Backlog"
    cols[1]["wip_limit"], cols[1]["min_cards"] = 3, 1
    res = admin_client.put(f"/api/agile/boards/{board_id}/columns", json={"columns": cols, "version": board["version"]})
    assert res.status_code == 200, res.text
    after = res.json()
    assert [c["id"] for c in after["columns"]] == ids_before
    assert after["columns"][0]["name"] == "Backlog"
    assert after["columns"][1]["wip_limit"] == 3

    board, cols = _columns(admin_client, board_id)
    dup = [dict(c) for c in cols]
    dup[0]["statuses"] = dup[0]["statuses"] + ["Done"]
    res = admin_client.put(f"/api/agile/boards/{board_id}/columns", json={"columns": dup, "version": board["version"]})
    assert res.status_code == 422
    assert "one column only" in res.json()["detail"][0]["msg"]

    not_done_last = [dict(c) for c in cols]
    not_done_last[-1]["category"] = "in_progress"
    res = admin_client.put(f"/api/agile/boards/{board_id}/columns",
                           json={"columns": not_done_last, "version": board["version"]})
    assert res.status_code == 422

    unknown = [dict(c) for c in cols]
    unknown[0]["statuses"] = ["No Such Status"]
    res = admin_client.put(f"/api/agile/boards/{board_id}/columns", json={"columns": unknown, "version": board["version"]})
    assert res.status_code == 422

    bad_wip = [dict(c) for c in cols]
    bad_wip[1]["wip_limit"], bad_wip[1]["min_cards"] = 1, 4
    res = admin_client.put(f"/api/agile/boards/{board_id}/columns", json={"columns": bad_wip, "version": board["version"]})
    assert res.status_code == 422


def test_unmapped_status_is_reported_not_lost(admin_client):
    """An issue whose status sits in no column is hidden from the columns, but the
    board lists it so it is not silently lost."""
    project, board_id = _project(admin_client, "Unmapped Suite")
    board, cols = _columns(admin_client, board_id)
    cols[0]["statuses"] = [s for s in cols[0]["statuses"] if s != "Deferred"]
    res = admin_client.put(f"/api/agile/boards/{board_id}/columns", json={"columns": cols, "version": board["version"]})
    assert res.status_code == 200, res.text
    story = _issue(admin_client, project["id"], "Parked", status="Deferred")
    sprint = _sprint(admin_client, board_id)
    _add(admin_client, sprint["id"], story["id"])
    view = admin_client.get(f"/api/agile/boards/{board_id}/view?sprint_id={sprint['id']}").json()
    assert [c["id"] for c in view["unmapped"]] == [story["id"]]
    assert all(story["id"] not in [c["id"] for c in col["cards"]] for col in view["columns"])


def test_the_rightmost_column_decides_done(admin_client):
    """Moving "Resolved" out of the last column makes resolved issues unfinished
    for sprint completion, whatever its status category says."""
    project, board_id = _project(admin_client, "Rightmost Suite")
    board, cols = _columns(admin_client, board_id)
    cols[-1]["statuses"] = [s for s in cols[-1]["statuses"] if s != "Resolved"]
    cols[-2]["statuses"] = cols[-2]["statuses"] + ["Resolved"]
    res = admin_client.put(f"/api/agile/boards/{board_id}/columns", json={"columns": cols, "version": board["version"]})
    assert res.status_code == 200, res.text
    a, b = _issue(admin_client, project["id"], "Resolved one"), _issue(admin_client, project["id"], "Done one")
    sprint = _sprint(admin_client, board_id)
    _add(admin_client, sprint["id"], a["id"], b["id"])
    _start(admin_client, sprint["id"])
    _set_status(admin_client, a["id"], "Resolved")
    _set_status(admin_client, b["id"], "Done")
    assert _complete(admin_client, sprint["id"]).status_code == 200
    report = admin_client.get(f"/api/agile/reports/sprint/{sprint['id']}").json()
    assert [r["id"] for r in report["completed"]] == [b["id"]]
    assert [r["id"] for r in report["not_completed"]] == [a["id"]]


# --- hierarchy -------------------------------------------------------------------

def test_hierarchy_rules(admin_client):
    project, _board = _project(admin_client, "Hierarchy Suite")
    other, _ = _project(admin_client, "Hierarchy Elsewhere")
    epic = _issue(admin_client, project["id"], "Epic", "Epic")
    story = _issue(admin_client, project["id"], "Story", epic_id=epic["id"])
    sub = _issue(admin_client, project["id"], "Sub", "Sub-task", parent_id=story["id"])
    assert _row(sub["id"]).epic_id == epic["id"]

    def relink(item, **body):
        item = admin_client.get(f"/api/bugs/{item['id']}").json()
        return admin_client.put(f"/api/agile/work-items/{item['id']}/hierarchy", json={"version": item["version"], **body})

    assert relink(story, parent_id=sub["id"]).status_code == 422          # only Sub-tasks have parents
    assert relink(sub, parent_id=epic["id"]).status_code == 422           # a Sub-task's parent is an issue
    assert relink(sub, parent_id=sub["id"]).status_code == 422            # never itself
    assert relink(story, epic_id=story["id"]).status_code == 422          # the epic link targets an Epic
    foreign_epic = _issue(admin_client, other["id"], "Foreign epic", "Epic")
    assert relink(story, epic_id=foreign_epic["id"]).status_code == 422   # of the same project
    res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Orphan", "item_type": "Sub-task",
    })
    assert res.status_code == 422
    assert "needs a parent" in res.json()["detail"][0]["msg"]

    second_epic = _issue(admin_client, project["id"], "Second epic", "Epic")
    assert relink(story, epic_id=second_epic["id"]).status_code == 200
    assert _row(sub["id"]).epic_id == second_epic["id"]                   # Sub-tasks follow


def test_deleting_an_issue_deletes_its_subtasks(admin_client):
    project, _board = _project(admin_client, "Delete Suite")
    story = _issue(admin_client, project["id"], "Story")
    subs = [_issue(admin_client, project["id"], f"Sub {n}", "Sub-task", parent_id=story["id"]) for n in (1, 2)]
    res = admin_client.delete(f"/api/bugs/{story['id']}")
    assert res.status_code == 200
    assert "2 sub-task" in res.json()["message"]
    for sub in subs:
        assert admin_client.get(f"/api/bugs/{sub['id']}").status_code == 404


def test_type_conversion_keeps_subtasks_and_refuses_epic(admin_client):
    project, _board = _project(admin_client, "Convert Suite")
    story = _issue(admin_client, project["id"], "Story")
    sub = _issue(admin_client, project["id"], "Sub", "Sub-task", parent_id=story["id"])
    res = admin_client.put(f"/api/bugs/{story['id']}", json={"item_type": "Task"})
    assert res.status_code == 200, res.text
    assert _row(sub["id"]).parent_id == story["id"]
    res = admin_client.put(f"/api/bugs/{story['id']}", json={"item_type": "Epic"})
    assert res.status_code == 422


def test_hierarchy_view_returns_the_epic_tree(admin_client):
    project, _board = _project(admin_client, "Tree Suite")
    epic = _issue(admin_client, project["id"], "Checkout", "Epic")
    story = _issue(admin_client, project["id"], "Pay by card", epic_id=epic["id"], story_points=5)
    sub = _issue(admin_client, project["id"], "Card form", "Sub-task", parent_id=story["id"])
    loose = _bug(admin_client, project["id"], "Loose bug")
    closed = _bug(admin_client, project["id"], "Closed bug", status="Closed")
    tree = admin_client.get(f"/api/agile/projects/{project['id']}/hierarchy").json()
    assert [e["id"] for e in tree["epics"]] == [epic["id"]]
    assert tree["epics"][0]["progress"]["child_count"] == 1
    assert tree["epics"][0]["progress"]["total_estimate"] == 5
    issues = {i["id"]: i for i in tree["issues"]}
    assert set(issues) == {story["id"], loose["id"], closed["id"]}
    assert issues[story["id"]]["epic_id"] == epic["id"]
    assert issues[story["id"]]["subtask_count"] == 1
    assert [s["id"] for s in tree["subtasks"]] == [sub["id"]]
    assert tree["unparented_total"] == 2
    open_only = admin_client.get(f"/api/agile/projects/{project['id']}/hierarchy?include_done=false").json()
    assert closed["id"] not in {i["id"] for i in open_only["issues"]}
    found = admin_client.get(f"/api/agile/projects/{project['id']}/hierarchy?q=loose").json()
    assert [i["id"] for i in found["issues"]] == [loose["id"]]


@pytest.fixture
def test_project_and_user(db_session):
    """Create a project and user for tests."""
    project = Project(name="TestProj", color="#000", agile_enabled=True)
    db_session.add(project)
    db_session.flush()
    
    user = User(email="test@ex.com", name="Test", role=ROLE_ADMIN, password_hash=hash_password("p"))
    db_session.add(user)
    db_session.flush()
    
    return project, user


@pytest.fixture
def task_project(db_session):
    """Create the required project for standalone task flag tests."""
    project = Project(name="TaskProj", color="#111", agile_enabled=True)
    db_session.add(project)
    db_session.flush()
    return project


class TestValidateStoryReadyRequirements:
    """Tests for _validate_story_ready_requirements helper."""

    def test_returns_false_when_description_missing(self, db_session, test_project_and_user):
        """Returns False if Story has empty description."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=user.id,
            start_date=date(2026, 1, 1), due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        # Manually set description to empty
        story.description = ""
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_false_when_no_priority(self, db_session, test_project_and_user):
        """Returns False if Story has no priority."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=user.id,
            start_date=date(2026, 1, 1), due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        # The CHECK constraint forbids storing an empty priority, so blank it
        # on the transient object only.
        with db_session.no_autoflush:
            story.priority = ""
            assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_false_when_no_story_points(self, db_session, test_project_and_user):
        """Returns False if Story has no story points."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=None, owner_id=user.id,
            start_date=date(2026, 1, 1), due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_false_when_no_owner_or_assignees(self, db_session, test_project_and_user):
        """Returns False if Story has neither owner nor assignees."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=None,
            start_date=date(2026, 1, 1), due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_false_when_no_start_date(self, db_session, test_project_and_user):
        """Returns False if Story has no start date."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=user.id,
            start_date=None, due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_false_when_no_due_date(self, db_session, test_project_and_user):
        """Returns False if Story has no due date."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=user.id,
            start_date=date(2026, 1, 1), due_date=None
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_false_when_no_acceptance_criteria(self, db_session, test_project_and_user):
        """Returns False if Story has no acceptance criteria."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=user.id,
            start_date=date(2026, 1, 1), due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        # No acceptance criterion added
        assert not _validate_story_ready_requirements(db_session, story)

    def test_returns_true_when_all_requirements_met(self, db_session, test_project_and_user):
        """Returns True if Story has all mandatory fields."""
        project, user = test_project_and_user
        
        story = Bug(
            item_type="Story", title="S", description="D", project_id=project.id,
            priority="High", story_points=5, owner_id=user.id,
            start_date=date(2026, 1, 1), due_date=date(2026, 12, 31)
        )
        db_session.add(story)
        db_session.flush()
        
        crit = AcceptanceCriterion(bug_id=story.id, description="C")
        db_session.add(crit)
        db_session.flush()
        
        assert _validate_story_ready_requirements(db_session, story)


class TestApplyTaskFlagUpdates:
    """Tests for _apply_task_flag_updates helper - extensive edge cases."""

    def test_apply_mandatory_flag_true(self, db_session, task_project):
        """Sets mandatory flag to True."""
        task = Bug(item_type="Sub-task", title="T", description="D", project_id=task_project.id,
                   mandatory=False, blocked=False)
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, mandatory=True)
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.mandatory is True
        assert blocked_changed is False

    def test_apply_mandatory_flag_false(self, db_session, task_project):
        """Sets mandatory flag to False."""
        task = Bug(item_type="Sub-task", title="T", description="D", project_id=task_project.id,
                   mandatory=True, blocked=False)
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, mandatory=False)
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.mandatory is False
        assert blocked_changed is False

    def test_apply_blocked_flag_true(self, db_session, task_project):
        """Sets blocked flag to True with reason."""
        task = Bug(item_type="Sub-task", title="T", description="D", project_id=task_project.id,
                   blocked=False, blocked_reason="")
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, blocked=True, blocked_reason="Waiting for approval")
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.blocked is True
        assert task.blocked_reason == "Waiting for approval"
        assert blocked_changed is True

    def test_apply_blocked_flag_false_clears_reason(self, db_session, task_project):
        """Sets blocked flag to False and clears reason."""
        task = Bug(item_type="Sub-task", title="T", description="D", project_id=task_project.id,
                   blocked=True, blocked_reason="Dependency")
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, blocked=False)
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.blocked is False
        assert task.blocked_reason == ""
        assert blocked_changed is True

    def test_apply_blocked_reason_update(self, db_session, task_project):
        """Updates reason without changing blocked status."""
        task = Bug(item_type="Sub-task", title="T", description="D", project_id=task_project.id,
                   blocked=True, blocked_reason="Old")
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, blocked_reason="New reason")
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.blocked_reason == "New reason"
        assert task.blocked is True
        assert blocked_changed is False  # No change to blocked flag itself

    def test_no_change_when_no_fields_provided(self, db_session, task_project):
        """Returns False when no fields are being updated."""
        task = Bug(item_type="Sub-task", title="T", description="D", project_id=task_project.id,
                   mandatory=True, blocked=False)
        db_session.add(task)
        db_session.flush()
        original_mandatory = task.mandatory
        
        payload = WorkItemTaskFlagsIn(version=1)  # All fields None
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.mandatory is original_mandatory
        assert task.blocked is False
        assert blocked_changed is False

    def test_multiple_flags_at_once(self, db_session, task_project):
        """Updates multiple flags simultaneously."""
        task = Bug(
            item_type="Sub-task", title="T", description="D", project_id=task_project.id,
            mandatory=False, blocked=False, blocked_reason=""
        )
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(
            version=1, mandatory=True, blocked=True, blocked_reason="Complex dep"
        )
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.mandatory is True
        assert task.blocked is True
        assert task.blocked_reason == "Complex dep"
        assert blocked_changed is True

    def test_unblock_and_set_mandatory(self, db_session, task_project):
        """Unblock an item and set mandatory flag in one call."""
        task = Bug(
            item_type="Sub-task", title="T", description="D", project_id=task_project.id,
            mandatory=False, blocked=True, blocked_reason="Old issue"
        )
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, mandatory=True, blocked=False)
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.mandatory is True
        assert task.blocked is False
        assert task.blocked_reason == ""
        assert blocked_changed is True

    def test_update_reason_only(self, db_session, task_project):
        """Update only the blocked reason without touching flags."""
        task = Bug(
            item_type="Sub-task", title="T", description="D", project_id=task_project.id,
            mandatory=True, blocked=True, blocked_reason="Initial"
        )
        db_session.add(task)
        db_session.flush()
        
        payload = WorkItemTaskFlagsIn(version=1, blocked_reason="Updated reason")
        blocked_changed = _apply_task_flag_updates(task, payload)
        
        assert task.mandatory is True  # Unchanged
        assert task.blocked is True  # Unchanged
        assert task.blocked_reason == "Updated reason"
        assert blocked_changed is False


# --- item-field routes: ready-for-sprint, dates, task flags, acceptance criteria ---

def _admin_id(client):
    return client.get("/api/auth/me").json()["id"]


def test_ready_for_sprint_needs_every_mandatory_field_and_a_current_version(admin_client):
    project, _board = _project(admin_client, "Ready Gate")
    story = _issue(admin_client, project["id"], "Gate", description="why", story_points=3,
                   start_date="2026-01-01", end_date="2026-01-09", assignee_ids=[_admin_id(admin_client)])
    url = f"/api/agile/work-items/{story['id']}/ready-for-sprint"

    # No acceptance criterion yet.
    refused = admin_client.put(url, json={"ready_for_sprint": True, "version": story["version"]})
    assert refused.status_code == 422
    admin_client.post(f"/api/agile/work-items/{story['id']}/acceptance-criteria", json={"description": "It works"})

    assert admin_client.put(url, json={"ready_for_sprint": True, "version": story["version"] + 5}).status_code == 409
    ready = admin_client.put(url, json={"ready_for_sprint": True, "version": story["version"]})
    assert ready.status_code == 200, ready.text
    assert ready.json()["ready_for_sprint"] is True
    # Clearing the flag never needs the requirements.
    cleared = admin_client.put(url, json={"ready_for_sprint": False, "version": ready.json()["version"]})
    assert cleared.json()["ready_for_sprint"] is False


def test_ready_for_sprint_is_for_stories_only(admin_client):
    project, _board = _project(admin_client, "Ready Types")
    epic = _issue(admin_client, project["id"], "An epic", item_type="Epic")
    res = admin_client.put(f"/api/agile/work-items/{epic['id']}/ready-for-sprint",
                           json={"ready_for_sprint": True, "version": epic["version"]})
    assert res.status_code == 422
    missing = admin_client.put("/api/agile/work-items/999999/ready-for-sprint",
                               json={"ready_for_sprint": True, "version": 1})
    assert missing.status_code == 404


def test_work_item_dates_are_changed_only_through_the_dates_route(admin_client):
    project, _board = _project(admin_client, "Dates")
    story = _issue(admin_client, project["id"], "Dated")
    url = f"/api/agile/work-items/{story['id']}/dates"

    stale = admin_client.put(url, json={"start_date": "2026-02-01", "end_date": "2026-02-09", "version": story["version"] + 3})
    assert stale.status_code == 409
    res = admin_client.put(url, json={"start_date": "2026-02-01", "end_date": "2026-02-09", "version": story["version"]})
    assert res.status_code == 200, res.text
    assert (res.json()["start_date"], res.json()["end_date"]) == ("2026-02-01", "2026-02-09")
    assert admin_client.put(url, json={"start_date": "soon", "version": res.json()["version"]}).status_code == 422
    assert admin_client.put("/api/agile/work-items/999999/dates", json={"version": 1}).status_code == 404


def test_task_flags_require_a_reason_and_audit_each_change(admin_client):
    project, _board = _project(admin_client, "Flags")
    story = _issue(admin_client, project["id"], "Flaggable")
    url = f"/api/agile/work-items/{story['id']}/task-flags"

    assert admin_client.put(url, json={"blocked": True, "version": story["version"]}).status_code == 422
    assert admin_client.put(url, json={"mandatory": True, "version": story["version"] + 2}).status_code == 409
    blocked = admin_client.put(url, json={"blocked": True, "blocked_reason": "Waiting on API",
                                          "mandatory": True, "version": story["version"]})
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["blocked"] is True
    unblocked = admin_client.put(url, json={"blocked": False, "version": blocked.json()["version"]})
    assert unblocked.json()["blocked"] is False
    actions = {a["action"] for a in admin_client.get("/api/audit").json()}
    assert {"item_blocked", "item_unblocked", "mandatory_changed"} <= actions

    epic = _issue(admin_client, project["id"], "Not flaggable", item_type="Epic")
    refused = admin_client.put(f"/api/agile/work-items/{epic['id']}/task-flags",
                               json={"mandatory": True, "version": epic["version"]})
    assert refused.status_code == 422


def test_acceptance_criteria_lifecycle(admin_client):
    project, _board = _project(admin_client, "Criteria")
    story = _issue(admin_client, project["id"], "With criteria")
    base = f"/api/agile/work-items/{story['id']}/acceptance-criteria"

    created = admin_client.post(base, json={"description": "  Shows the total  ", "required": False})
    assert created.status_code == 201, created.text
    criterion = created.json()
    assert criterion["description"] == "Shows the total"
    assert admin_client.post(base, json={"description": "   "}).status_code == 422
    assert [c["id"] for c in admin_client.get(base).json()] == [criterion["id"]]

    url = f"/api/agile/acceptance-criteria/{criterion['id']}"
    updated = admin_client.put(url, json={"met": True, "required": True})
    assert updated.status_code == 200
    assert (updated.json()["met"], updated.json()["required"]) == (True, True)
    assert admin_client.delete(url).status_code == 200
    assert admin_client.get(base).json() == []
    assert admin_client.put(url, json={"met": True}).status_code == 404
    assert admin_client.delete(url).status_code == 404
    assert admin_client.get("/api/agile/work-items/999999/acceptance-criteria").status_code == 404
    assert admin_client.post("/api/agile/work-items/999999/acceptance-criteria",
                             json={"description": "x"}).status_code == 404


# --- board routes: lookups, WIP limits, estimates, taxonomy, quick filters ---------------

def _move(client, item_id, status, **extra):
    version = client.get(f"/api/bugs/{item_id}").json().get("version", 1)
    return client.post(f"/api/agile/work-items/{item_id}/transition",
                       json={"to_status": status, "version": version, **extra})


def test_board_lookups_hide_other_boards_and_disabled_projects(admin_client):
    project, board_id = _project(admin_client, "Lookups A")
    other, other_board = _project(admin_client, "Lookups B")
    foreign_sprint = _sprint(admin_client, other_board, "Elsewhere")

    assert admin_client.get("/api/agile/boards/999999/view").status_code == 404
    assert admin_client.get("/api/agile/boards/999999/planning").status_code == 404
    assert admin_client.get(f"/api/agile/boards/{board_id}/view?sprint_id=999999").status_code == 404
    assert admin_client.get(f"/api/agile/boards/{board_id}/view?sprint_id={foreign_sprint['id']}").status_code == 404
    moved = admin_client.post(f"/api/agile/boards/{board_id}/move",
                              json={"item_ids": [1], "sprint_id": foreign_sprint["id"]})
    assert moved.status_code == 404
    assert admin_client.get("/api/agile/projects/999999/hierarchy").status_code == 404
    assert admin_client.get("/api/agile/sprints/999999").status_code == 404
    assert admin_client.get("/api/agile/reports/sprint/999999").status_code == 404

    story = _issue(admin_client, project["id"], "Before disable")
    sprint = _sprint(admin_client, board_id, "Orphan")
    assert admin_client.post(f"/api/agile/projects/{project['id']}/disable").status_code == 200
    for url in (f"/api/agile/boards/{board_id}", f"/api/agile/boards/{board_id}/view",
                f"/api/agile/sprints/{sprint['id']}", f"/api/agile/projects/{project['id']}/hierarchy",
                f"/api/agile/reports/sprint/{sprint['id']}"):
        assert admin_client.get(url).status_code == 404, url
    assert admin_client.get(f"/api/agile/reports/burndown?sprint_id={sprint['id']}").status_code == 404
    assert story["id"]
    assert other["id"]


def test_transitions_enforce_versions_and_wip_limits(admin_client):
    project, board_id = _project(admin_client, "Wip Limits")
    first = _issue(admin_client, project["id"], "First in progress")
    second = _issue(admin_client, project["id"], "Second in progress")
    board, cols = _columns(admin_client, board_id)
    cols[1]["wip_limit"], cols[1]["min_cards"], cols[1]["wip_enforcement"] = 1, 1, "block"
    assert admin_client.put(f"/api/agile/boards/{board_id}/columns",
                            json={"columns": cols, "version": board["version"]}).status_code == 200

    stale = admin_client.post(f"/api/agile/work-items/{first['id']}/transition",
                              json={"to_status": "In Progress", "version": first["version"] + 9})
    assert stale.status_code == 409
    assert _move(admin_client, 999999, "In Progress").status_code == 404
    assert _move(admin_client, first["id"], "In Progress").status_code == 200
    assert _move(admin_client, first["id"], "No Such Status").status_code == 422

    blocked = _move(admin_client, second["id"], "In Progress")
    assert blocked.status_code == 400
    assert "WIP limit" in blocked.json()["detail"]
    no_reason = _move(admin_client, second["id"], "In Progress", override_wip=True)
    assert no_reason.status_code == 422
    assert "override_reason" in str(no_reason.json()["detail"])
    assert _move(admin_client, second["id"], "In Progress", override_wip=True,
                 override_reason="Customer escalation").status_code == 200

    board, cols = _columns(admin_client, board_id)
    cols[1]["wip_enforcement"] = "warn"
    assert admin_client.put(f"/api/agile/boards/{board_id}/columns",
                            json={"columns": cols, "version": board["version"]}).status_code == 200
    third = _issue(admin_client, project["id"], "Third in progress")
    warned = _move(admin_client, third["id"], "In Progress")
    assert warned.status_code == 400
    assert "acknowledge" in warned.json()["detail"]
    assert _move(admin_client, third["id"], "In Progress", acknowledged=True).status_code == 200


def test_a_board_without_a_default_cannot_transition(admin_client, db_session):
    project, board_id = _project(admin_client, "No Default")
    story = _issue(admin_client, project["id"], "Stranded")
    from app.models import Board

    db_session.query(Board).filter_by(id=board_id).update({"is_default": False})
    db_session.commit()
    assert _move(admin_client, story["id"], "In Progress").status_code == 404


def test_estimates_and_taxonomy_reject_stale_or_invalid_edits(admin_client):
    project, _board_id = _project(admin_client, "Estimates")
    story = _issue(admin_client, project["id"], "Estimable")
    epic = _issue(admin_client, project["id"], "Not estimable", item_type="Epic")

    stale = admin_client.put(f"/api/agile/work-items/{story['id']}/estimate",
                             json={"story_points": 5, "version": story["version"] + 3})
    assert stale.status_code == 409
    refused = admin_client.put(f"/api/agile/work-items/{epic['id']}/estimate",
                               json={"story_points": 5, "version": epic["version"]})
    assert refused.status_code == 422

    stale_tax = admin_client.put(f"/api/agile/work-items/{story['id']}/taxonomy",
                                 json={"label_ids": [], "version": story["version"] + 3})
    assert stale_tax.status_code == 409
    unknown = admin_client.put(f"/api/agile/work-items/{story['id']}/taxonomy",
                               json={"label_ids": [999999], "component_ids": [999999], "version": story["version"]})
    assert unknown.status_code == 422


def test_quick_filters_can_be_edited_and_removed(admin_client):
    _project_row, board_id = _project(admin_client, "Quick Filters")
    created = admin_client.post(f"/api/agile/boards/{board_id}/quick-filters",
                                json={"name": "Mine", "filter_json": {"assignee": "me"}})
    assert created.status_code == 201, created.text
    qf = created.json()
    url = f"/api/agile/quick-filters/{qf['id']}"

    renamed = admin_client.put(url, json={"name": "Only mine", "is_shared": False, "position": 5,
                                          "filter_json": {"assignee": "me"}, "version": qf["version"]})
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["name"], renamed.json()["is_shared"]) == ("Only mine", False)
    assert admin_client.put(url, json={"name": "Late", "version": qf["version"]}).status_code == 409
    assert admin_client.put("/api/agile/quick-filters/999999", json={"name": "x", "version": 1}).status_code == 404
    assert admin_client.delete(url).status_code == 200
    assert admin_client.delete(url).status_code == 404
