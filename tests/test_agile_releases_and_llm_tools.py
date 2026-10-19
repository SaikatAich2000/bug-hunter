"""Basic sanity tests for release readiness/roadmap, reporting follow-on and
the Sleuth LLM-driven tool-calling agent.
"""
from __future__ import annotations

import pytest


def _setup_project_with_board(admin_client, name="Release Suite"):
    res = admin_client.post("/api/projects", json={"name": name, "description": "", "color": "#c9764f"})
    assert res.status_code == 201, res.text
    project = res.json()
    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert enable_res.status_code == 200, enable_res.text
    return project, enable_res.json()["board_id"]


def _create_story(admin_client, project_id, title, **extra):
    body = {"project_id": project_id, "title": title, "item_type": "story"}
    body.update(extra)
    res = admin_client.post("/api/agile/work-items", json=body)
    assert res.status_code == 201, res.text
    return res.json()


# --- Release readiness / roadmap ---

def test_release_action_and_double_release_rejected(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Release Gate Suite")

    v_res = admin_client.post("/api/agile/releases", json={"project_id": project["id"], "name": "v1.0"})
    assert v_res.status_code == 201, v_res.text
    version = v_res.json()

    # No work items associated with this release — readiness has only the
    # missing-release-date warning (not a hard error), so it should succeed.
    release_res = admin_client.post(f"/api/agile/releases/{version['id']}/release", json={
        "version": version["version"],
    })
    assert release_res.status_code == 200, release_res.text
    released = release_res.json()
    assert released["status"] == "released"

    # Releasing an already-released version must be rejected.
    second_res = admin_client.post(f"/api/agile/releases/{version['id']}/release", json={
        "version": released["version"],
    })
    assert second_res.status_code == 409


def test_release_360_and_roadmap(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Roadmap Suite")
    epic_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Roadmap Epic", "item_type": "epic",
    })
    assert epic_res.status_code == 201, epic_res.text
    epic = epic_res.json()
    admin_client.put(f"/api/agile/epics/{epic['id']}", json={
        "start_date": "2026-01-01", "target_date": "2026-03-01", "version": 1,
    })

    v_res = admin_client.post("/api/agile/releases", json={
        "project_id": project["id"], "name": "v2.0",
        "start_date": "2026-01-01", "release_date": "2026-02-01",
    })
    version = v_res.json()

    r360_res = admin_client.get(f"/api/agile/releases/{version['id']}/360")
    assert r360_res.status_code == 200, r360_res.text
    assert "readiness" in r360_res.json()

    roadmap_res = admin_client.get(f"/api/agile/roadmap?project_id={project['id']}")
    assert roadmap_res.status_code == 200, roadmap_res.text
    roadmap = roadmap_res.json()
    assert any(e["id"] == epic["id"] for e in roadmap["epics"])
    assert any(r["id"] == version["id"] for r in roadmap["releases"])


def test_reporting_follow_on_smoke(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Follow-on Reports Suite")
    item = _create_story(admin_client, project["id"], "Reported story", story_points=3)
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Closed Sprint", "start_date": "2026-01-01", "end_date": "2026-01-07",
    }).json()
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]})
    admin_client.post(f"/api/agile/work-items/{item['id']}/transition", json={
        "to_status": "Done", "version": item["version"],
    })
    complete_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/complete", json={
        "closing_note": "wrapped up", "version": sprint["version"] + 1,
        "default_destination": "backlog",
    })
    assert complete_res.status_code == 200, complete_res.text

    velocity_res = admin_client.get(f"/api/agile/reports/velocity?board_id={board_id}&sprint_count=5")
    assert velocity_res.status_code == 200, velocity_res.text
    assert len(velocity_res.json()["points"]) == 1

    cfd_res = admin_client.get(
        f"/api/agile/reports/cumulative-flow?board_id={board_id}&date_from=2026-01-01&date_to=2026-01-07"
    )
    assert cfd_res.status_code == 200, cfd_res.text
    assert len(cfd_res.json()["points"]) == 7

    cc_res = admin_client.get(
        f"/api/agile/reports/control-chart?board_id={board_id}&date_from=2026-01-01&date_to=2026-01-07"
    )
    assert cc_res.status_code == 200, cc_res.text


# --- Sleuth LLM-driven tool-calling agent ---

def test_llm_tools_agent_disabled_by_default():
    from app.chatbot import llm_tools_agent
    assert llm_tools_agent.is_available() is False


def test_no_delete_tool_exists():
    from app.chatbot import tools
    assert not any("delete" in name for name in tools.ALL_TOOL_NAMES)
    for spec in tools.TOOL_SPECS:
        assert "delete" not in spec["function"]["name"]


def test_llm_tools_rejects_disallowed_tool_name(monkeypatch, admin_client):
    from app.chatbot import llm_tools_agent

    monkeypatch.setattr(llm_tools_agent, "call_llm_with_tools", lambda messages: {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "1", "function": {"name": "delete_everything", "arguments": "{}"}}],
    })

    from app.database import SessionLocal
    from app.models import User
    db = SessionLocal()
    try:
        actor = db.query(User).first()
        resp = llm_tools_agent.run("please help", db, actor)
    finally:
        db.close()
    assert resp.intent == "llm_tools_blocked"
    assert "blocked" in resp.blocks[0].payload["text"].lower()


def test_llm_tools_write_tool_stages_confirmation(monkeypatch, admin_client):
    from app.chatbot import llm_tools_agent
    from app.chatbot.memory import store as mem_store

    project_res = admin_client.post("/api/projects", json={"name": "LLM Tools Suite", "color": "#c9764f"})
    project = project_res.json()

    call_count = {"n": 0}

    def fake_call(messages):
        call_count["n"] += 1
        return {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "1", "function": {
                "name": "create_work_item",
                "arguments": f'{{"project_name": "{project["name"]}", "title": "New bug from Sleuth"}}',
            }}],
        }

    monkeypatch.setattr(llm_tools_agent, "call_llm_with_tools", fake_call)

    from app.database import SessionLocal
    from app.models import User
    db = SessionLocal()
    try:
        actor = db.query(User).filter(User.email == "admin@test.local").first()
        resp = llm_tools_agent.run("create a bug titled New bug from Sleuth", db, actor)
        assert resp.intent == "confirm_action"
        assert call_count["n"] == 1  # write tool call stops the loop immediately

        pending = mem_store.take_pending(actor.id)
        assert pending is not None
        assert pending["kind"] == "llm_tool_call"
        assert pending["tool"] == "create_work_item"

        confirm_resp = llm_tools_agent.confirm_pending_tool_call(db, actor, pending)
        assert confirm_resp.intent == "action_done"
    finally:
        db.close()


def test_llm_tools_provider_failure_returns_clear_error(monkeypatch, admin_client):
    from app.chatbot import llm_tools_agent

    monkeypatch.setattr(llm_tools_agent, "call_llm_with_tools", lambda messages: None)

    from app.database import SessionLocal
    from app.models import User
    db = SessionLocal()
    try:
        actor = db.query(User).first()
        resp = llm_tools_agent.run("hello", db, actor)
    finally:
        db.close()
    assert resp.intent == "llm_tools_unavailable"
    assert "unavailable" in resp.blocks[0].payload["text"].lower()


# --- release progress and readiness over real fix-version links ----------------------

def _release_fixture(db):
    from app.models import Bug, BugLink, Project, Version, WorkItemVersion

    project = Project(name="Release Unit", color="#123456", agile_enabled=True)
    db.add(project)
    db.flush()
    version = Version(project_id=project.id, name="v2", name_normalized="v2")
    empty = Version(project_id=project.id, name="v3", name_normalized="v3")
    db.add_all([version, empty])
    db.flush()
    done = Bug(project_id=project.id, title="Shipped", item_type="Story", status="Done", story_points=3)
    open_critical = Bug(project_id=project.id, title="Broken", item_type="Bug", status="New",
                        priority="Critical", story_points=2)
    unestimated = Bug(project_id=project.id, title="Vague", item_type="Task", status="In Progress")
    blocker = Bug(project_id=project.id, title="Blocker", item_type="Task", status="New")
    db.add_all([done, open_critical, unestimated, blocker])
    db.flush()
    for item in (done, open_critical, unestimated):
        db.add(WorkItemVersion(work_item_id=item.id, version_id=version.id, relation_type="fix"))
    db.add(BugLink(source_bug_id=blocker.id, target_bug_id=unestimated.id, link_type="blocks"))
    db.flush()
    return version, empty, done, blocker


def test_release_progress_counts_estimates_and_completion(db_session):
    from app.agile import releases

    version, empty, _done, _blocker = _release_fixture(db_session)
    assert releases.release_progress(db_session, version) == {
        "item_count": 3, "completed_count": 1, "total_estimate": 5.0,
        "completed_estimate": 3.0, "unestimated_count": 1,
    }
    assert releases.release_progress(db_session, empty)["item_count"] == 0
    many = releases._release_progress_many(db_session, [version, empty])
    assert many[version.id]["total_estimate"] == 5.0
    assert many[empty.id] == releases._empty_progress()
    assert releases._release_progress_many(db_session, []) == {}


def test_release_readiness_names_every_blocking_problem(db_session):
    from app.agile import releases

    version, empty, done, blocker = _release_fixture(db_session)
    checks = {c["key"]: c for c in releases.release_readiness(db_session, version)}
    assert checks["all_work_complete"]["passed"] is False
    assert "2 associated item(s)" in checks["all_work_complete"]["message"]
    assert "1 open critical bug(s)" in checks["no_open_critical_bugs"]["message"]
    assert "1 unresolved blocker(s)" in checks["no_unresolved_blockers"]["message"]
    assert checks["has_release_date"]["severity"] == "warning"

    with pytest.raises(releases.ReleaseError, match="Release is not ready"):
        releases.mark_released(db_session, version, None)
    empty.archived = True
    with pytest.raises(releases.ReleaseError, match="archived"):
        releases.mark_released(db_session, empty, None)
    empty.archived = False
    released = releases.mark_released(db_session, empty, None)
    assert released.status == "released"
    assert released.released_at is not None


# --- provider selection, transport and the bounded tool loop --------------------------

def _tool_call(name, arguments="{}", call_id="c1"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}]}


def _admin(db):
    from app.models import User

    return db.query(User).filter(User.email == "admin@test.local").first()


def test_llm_provider_prefers_groq_then_openrouter(monkeypatch):
    from app.chatbot import llm_tools_agent
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(Settings, "OPENROUTER_API_KEY", "")
    get_settings.cache_clear()
    assert llm_tools_agent._headers_and_url(get_settings()) is None
    assert llm_tools_agent.call_llm_with_tools([]) is None

    monkeypatch.setattr(Settings, "OPENROUTER_API_KEY", "or-key")
    get_settings.cache_clear()
    assert llm_tools_agent._headers_and_url(get_settings())[1] == "or-key"
    monkeypatch.setattr(Settings, "GROQ_API_KEY", "groq-key")
    get_settings.cache_clear()
    assert "groq" in llm_tools_agent._headers_and_url(get_settings())[0]

    monkeypatch.setattr(Settings, "SLEUTH_CLOUD_ENABLED", True)
    monkeypatch.setattr(Settings, "SLEUTH_LLM_TOOLS_ENABLED", True)
    get_settings.cache_clear()
    assert llm_tools_agent.is_available() is True
    monkeypatch.setattr(Settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(Settings, "OPENROUTER_API_KEY", "")
    get_settings.cache_clear()
    assert llm_tools_agent.is_available() is False
    get_settings.cache_clear()


def test_llm_call_returns_the_assistant_message_or_none(monkeypatch):
    import httpx

    from app.chatbot import llm_tools_agent
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GROQ_API_KEY", "groq-key")
    get_settings.cache_clear()
    sent = {}

    class _Reply:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

    def _post(url, **kwargs):
        sent.update(kwargs)
        return _Reply()

    monkeypatch.setattr(httpx, "post", _post)
    assert llm_tools_agent.call_llm_with_tools([{"role": "user", "content": "x"}])["content"] == "hi"
    assert sent["headers"]["Authorization"] == "Bearer groq-key"
    assert sent["json"]["tool_choice"] == "auto"

    def _down(url, **kwargs):
        raise httpx.ConnectError("provider down")

    monkeypatch.setattr(httpx, "post", _down)
    assert llm_tools_agent.call_llm_with_tools([]) is None
    get_settings.cache_clear()


def test_tool_call_arguments_are_parsed_defensively():
    from app.chatbot import llm_tools_agent

    parse = llm_tools_agent._parse_tool_call
    assert parse({"function": {"name": "t", "arguments": '{"a": 1}'}}) == ("t", {"a": 1})
    assert parse({"function": {"name": "t", "arguments": {"a": 2}}}) == ("t", {"a": 2})
    assert parse({"function": {"name": "t", "arguments": "{broken"}}) == ("t", {})
    assert parse({"function": {"name": "t", "arguments": 5}}) == ("t", {})
    assert parse({}) == ("", {})


def test_read_tools_feed_the_next_round_and_the_loop_is_bounded(monkeypatch, admin_client):
    from app.chatbot import llm_tools_agent
    from app.database import SessionLocal

    admin_client.post("/api/projects", json={"name": "Loop Project", "color": "#c9764f"})
    rounds = []
    script = [_tool_call("search_work_items", '{"query": "x"}'), {"role": "assistant", "content": "Found nothing."}]

    def _llm(messages):
        rounds.append([m["role"] for m in messages])
        return script.pop(0)

    monkeypatch.setattr(llm_tools_agent, "call_llm_with_tools", _llm)
    with SessionLocal() as db:
        answer = llm_tools_agent.run("what is open?", db, _admin(db))
    assert answer.intent == "llm_tools_answer"
    assert rounds[1][-2:] == ["assistant", "tool"]

    monkeypatch.setattr(llm_tools_agent, "call_llm_with_tools",
                        lambda messages: _tool_call("search_work_items", '{"query": "x"}'))
    with SessionLocal() as db:
        assert llm_tools_agent.run("again", db, _admin(db)).intent == "llm_tools_max_rounds"

    monkeypatch.setattr(llm_tools_agent, "call_llm_with_tools", lambda messages: {"content": "   "})
    with SessionLocal() as db:
        empty = llm_tools_agent.run("hello", db, _admin(db))
    assert empty.blocks[0].payload["text"] == "I'm not sure how to help with that."


def test_a_failing_read_tool_reports_an_error_to_the_model(monkeypatch, admin_client):
    from app.chatbot import llm_tools_agent
    from app.chatbot.tools import ToolError
    from app.database import SessionLocal

    def _refused(db, actor, args):
        raise ToolError("no access")

    def _broken(db, actor, args):
        raise RuntimeError("tool bug")

    monkeypatch.setitem(llm_tools_agent.TOOL_REGISTRY, "get_backlog", _refused)
    monkeypatch.setitem(llm_tools_agent.TOOL_REGISTRY, "list_sprints", _broken)
    with SessionLocal() as db:
        actor = _admin(db)
        assert llm_tools_agent._call_tool_safely("get_backlog", db, actor, {}) == {"error": "no access"}
        assert llm_tools_agent._call_tool_safely("list_sprints", db, actor, {}) == {
            "error": "That lookup failed unexpectedly."}


def test_confirming_a_staged_tool_call_handles_every_outcome(monkeypatch, admin_client):
    from app.chatbot import llm_tools_agent
    from app.database import SessionLocal

    project = admin_client.post("/api/projects", json={"name": "Confirm Project", "color": "#c9764f"}).json()
    bug = admin_client.post("/api/bugs", json={"project_id": project["id"], "title": "Confirm me"}).json()

    with SessionLocal() as db:
        actor = _admin(db)
        blocked = llm_tools_agent.confirm_pending_tool_call(db, actor, {"tool": "nuke", "args": {}})
        assert blocked.intent == "llm_tools_blocked"

        invalid = llm_tools_agent.confirm_pending_tool_call(
            db, actor, {"tool": "transition_work_item", "args": {"item_id": 999999, "to_status": "Done"}})
        assert invalid.intent == "action_invalid"

        moved = llm_tools_agent.confirm_pending_tool_call(
            db, actor, {"tool": "transition_work_item", "args": {"item_id": bug["id"], "to_status": "Done"}})
        assert moved.intent == "action_done"
        assign = {"tool": "assign_work_item", "args": {"item_id": bug["id"], "assignee_name": actor.name}}
        assert llm_tools_agent.confirm_pending_tool_call(db, actor, assign).intent == "action_done"
        assert llm_tools_agent.confirm_pending_tool_call(db, actor, assign).intent == "action_done"

        def _explode(db_, actor_, args):
            raise RuntimeError("boom")

        monkeypatch.setitem(llm_tools_agent.TOOL_REGISTRY, "add_comment", _explode)
        failed = llm_tools_agent.confirm_pending_tool_call(db, actor, {"tool": "add_comment", "args": {}})
        assert failed.intent == "error"

    actions = [a["action"] for a in admin_client.get("/api/audit").json()]
    assert actions.count("status_changed") == 1
    assert actions.count("assignees_added") == 1
