"""Tests for NLU scoping and related guards.

Unit-level coverage of NLU over-match handling, the classifier degenerate-input
guard, the shared permission helpers, and item-type scoping. DB-backed behavior
(item-type filtering end to end, get_event truncation) uses the client fixture.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.chatbot import classifier, nlu


def _ctx(projects=None, users=None):
    return nlu.Context(users=users or [], projects=projects or [])


# ---------------------------------------------------------------------------
# A named time window survives irregular internal whitespace.
# ---------------------------------------------------------------------------
def test_time_window_tolerates_extra_whitespace():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    for phrase in ("this week", "this  week", "this\tweek"):
        tw = nlu._parse_time_window(f"bugs from {phrase}", now=now)
        assert tw is not None, phrase
        assert tw.label == "this week"


# ---------------------------------------------------------------------------
# The free-text tail strips reporter/status/environment clauses.
# ---------------------------------------------------------------------------
def test_free_text_strips_trailing_filter_clauses():
    assert nlu._extract_text_search("bugs about crash reported by alice") == "crash"
    assert nlu._extract_text_search("issues about login with environment PROD") == "login"
    assert nlu._extract_text_search("bugs about timeout with status closed") == "timeout"


# ---------------------------------------------------------------------------
# A project named like a common word isn't matched without an explicit cue.
# ---------------------------------------------------------------------------
def test_project_named_open_not_matched_without_cue():
    ctx = _ctx(projects=[(1, "open", "Open")])
    pq = nlu.parse("show me open bugs", ctx)
    assert pq.project_ids == []           # "open" is a status word, not the project
    pq2 = nlu.parse("show bugs in project Open", ctx)
    assert pq2.project_ids == [1]         # explicit cue still resolves it


# ---------------------------------------------------------------------------
# Ambiguous status/environment words don't over-match.
# ---------------------------------------------------------------------------
def test_bare_later_is_not_a_status():
    assert nlu._extract_statuses("I'll look at this later") == []
    assert nlu._extract_statuses("mark it resolve later") == ["Resolve Later"]


def test_ambiguous_env_words_require_context():
    assert nlu._extract_environments("test the export") == []
    assert nlu._extract_environments("go live tomorrow") == []
    assert nlu._extract_environments("attach the local file") == []
    assert nlu._extract_environments("bugs in test") == ["UAT"]
    assert nlu._extract_environments("deploy to live") == ["PROD"]
    assert nlu._extract_environments("the local environment") == ["DEV"]
    # Unambiguous env words still work as before.
    assert nlu._extract_environments("bugs in prod") == ["PROD"]


# ---------------------------------------------------------------------------
# _RECENT_RE boundary precedence
# ---------------------------------------------------------------------------
def test_recent_re_word_boundaries():
    assert nlu._RECENT_RE.search("prehistory") is None
    assert nlu._RECENT_RE.search("what happenedx") is None
    assert nlu._RECENT_RE.search("what happened today") is not None
    assert nlu._RECENT_RE.search("show recent activity") is not None
    assert nlu._RECENT_RE.search("the audit log") is not None


# ---------------------------------------------------------------------------
# The classifier rejects degenerate number-only input.
# ---------------------------------------------------------------------------
def test_classifier_rejects_num_only_input():
    assert classifier.predict("is it 5?") is None
    assert classifier.predict("42") is None


def test_classifier_still_classifies_real_queries():
    pred = classifier.predict("show me all the open bugs")
    assert pred is not None and pred.intent


# ---------------------------------------------------------------------------
# Item-type scoping in the NLU
# ---------------------------------------------------------------------------
def test_item_types_detected_from_nouns():
    ctx = _ctx()
    assert nlu.parse("list tasks", ctx).item_types == ["Task"]
    assert nlu.parse("show requirements", ctx).item_types == ["Requirement"]
    assert nlu.parse("how many bugs", ctx).item_types == ["Bug"]
    assert nlu.parse("how many defects", ctx).item_types == ["Bug"]
    # Generic "items" stays type-agnostic.
    assert nlu.parse("list all items", ctx).item_types == []
    # Word-boundary: "multitask" / "debug" don't scope.
    assert nlu.parse("debug the multitask runner", ctx).item_types == []


# ---------------------------------------------------------------------------
# Item-type scope is applied to the list/count SQL (DB-backed).
# ---------------------------------------------------------------------------
def _texts(resp):
    return " ".join(b.payload.get("text", "") for b in resp.blocks if b.kind == "text")


def test_chat_count_scopes_to_item_type(client):
    from app.database import SessionLocal
    from app import models
    from app.chatbot import executor

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        proj = models.Project(name="Scope", description="")
        db.add(proj)
        db.commit()
        db.add_all([
            models.Bug(title="b", status="New", priority="Low", environment="DEV",
                       project_id=proj.id, reporter_id=admin.id, item_type="Bug"),
            models.Bug(title="t", status="New", priority="Low", environment="DEV",
                       project_id=proj.id, reporter_id=admin.id, item_type="Task"),
        ])
        db.commit()
        # 1 task, 1 bug, 2 items total → the type scope changes the count.
        assert "**1**" in _texts(executor.execute("how many tasks", db, admin))
        assert "**1**" in _texts(executor.execute("how many bugs", db, admin))
        assert "**2**" in _texts(executor.execute("how many items", db, admin))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Create-bug priority uses normalize_choice, matching REST behavior.
# ---------------------------------------------------------------------------
def test_create_bug_normalizes_lowercase_priority(client):
    from app.database import SessionLocal
    from app import models
    from app.chatbot import actions

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        proj = models.Project(name="P2", description="")
        db.add(proj)
        db.commit()
        plan = actions.ActionPlan(
            kind="create_bug", actor_user_id=admin.id, new_title="Lower pri",
            new_value="high", new_project_id=proj.id, summary_human="x",
        )
        resp = actions._apply_create_bug(db, plan, admin)
        assert resp.intent == "action_done"
        bug = db.query(models.Bug).filter_by(title="Lower pri").first()
        assert bug.priority == "High"   # "high" → canonical "High"
    finally:
        db.close()


def test_create_bug_rejects_bogus_priority(client):
    from app.database import SessionLocal
    from app import models
    from app.chatbot import actions

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        proj = models.Project(name="P3", description="")
        db.add(proj)
        db.commit()
        plan = actions.ActionPlan(
            kind="create_bug", actor_user_id=admin.id, new_title="Bad pri",
            new_value="ultra", new_project_id=proj.id, summary_human="x",
        )
        resp = actions._apply_create_bug(db, plan, admin)
        assert resp.intent == "action_error"
        assert "priority" in _texts(resp).lower()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# get_event surfaces items_truncated when the item list is capped.
# ---------------------------------------------------------------------------
def test_get_event_items_truncated(admin_client, monkeypatch):
    import app.routes.events as ev_mod
    from app.database import SessionLocal
    from app import models

    monkeypatch.setattr(ev_mod, "_EVENT_ITEMS_MAX", 1)
    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        proj = models.Project(name="EvP", description="")
        db.add(proj)
        db.commit()
        ev = models.Event(name="Sprint")
        db.add(ev)
        db.commit()
        for i in range(2):
            db.add(models.Bug(title=f"i{i}", status="New", priority="Low",
                              environment="DEV", project_id=proj.id,
                              reporter_id=admin.id, event_id=ev.id))
        db.commit()
        eid = ev.id
    finally:
        db.close()

    body = admin_client.get(f"/api/events/{eid}").json()
    assert body["items_truncated"] is True
    assert len(body["items"]) == 1


def test_get_event_not_truncated_for_small_event(admin_client):
    from app.database import SessionLocal
    from app import models

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        proj = models.Project(name="EvP2", description="")
        db.add(proj)
        db.commit()
        ev = models.Event(name="Tiny")
        db.add(ev)
        db.commit()
        db.add(models.Bug(title="solo", status="New", priority="Low",
                          environment="DEV", project_id=proj.id,
                          reporter_id=admin.id, event_id=ev.id))
        db.commit()
        eid = ev.id
    finally:
        db.close()

    body = admin_client.get(f"/api/events/{eid}").json()
    assert body["items_truncated"] is False
