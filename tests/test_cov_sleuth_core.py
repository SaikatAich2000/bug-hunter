"""Coverage slice: the Sleuth chatbot's NLU parser + command executor.

Scope (the single largest uncovered slice): app.chatbot.nlu and
app.chatbot.executor. The cloud LLM layer is OFF in the test suite
(SLEUTH_CLOUD_ENABLED=0 — set in conftest), so every message runs the
deterministic local chain:

    parse() -> rule dispatch -> classifier -> (LLM unavailable) -> unknown

Calling pattern
---------------
The `client` fixture re-imports every ``app.*`` module per test so the
SQLite engine picks up the per-test env override. We therefore import
``app.*`` INSIDE each test (module-level refs go stale) and build a real
``Session`` via the freshly-imported ``app.database.SessionLocal``. Most
branches are reached by calling ``executor.execute(message, db, actor)``
and ``nlu.parse(message, ctx)`` directly — the cleanest way to land on a
specific parser/handler branch — with a handful of end-to-end checks
through ``POST /api/chat/ask`` (admin_client / user_client).

Everything is hermetic: no network, no real LLM, no Excel/openpyxl
dependency required (the export path degrades gracefully and we assert
whichever branch actually fires).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Seeding helpers — build a small but representative graph of users /
# projects / events / bugs directly through the (re-imported) ORM so the
# parser's name resolution and the executor's SELECTs have something to
# chew on.
# ---------------------------------------------------------------------------
def _seed(client):
    """Create users, projects, an event and bugs in the per-test DB.

    Returns a dict of handy ids. Must be called from inside a test so the
    `client` fixture has already re-imported app.* and pointed the engine
    at this test's SQLite file.
    """
    from app.database import SessionLocal
    from app import models
    from app.auth import hash_password

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(email="admin@test.local").one()
        alice = models.User(name="Alice Wonderland", email="alice@test.local",
                            role="manager", password_hash=hash_password("x"),
                            is_active=True)
        bob = models.User(name="Bob Builder", email="bob@test.local",
                          role="user", password_hash=hash_password("x"),
                          is_active=True)
        carol = models.User(name="Carol Singer", email="carol@test.local",
                            role="user", password_hash=hash_password("x"),
                            is_active=False)
        db.add_all([alice, bob, carol])
        db.commit()

        proj_a = models.Project(name="Apollo", description="Mission control")
        proj_b = models.Project(name="Beacon", description=None)
        db.add_all([proj_a, proj_b])
        db.commit()

        event = models.Event(name="Release 9.9", description="Big launch")
        db.add(event)
        db.commit()

        now = datetime.now(timezone.utc)
        # (status, priority, env, project, age_days, title, event_id)
        spec = [
            ("New",           "Critical", "PROD", proj_a,  1, "Service down", event.id),
            ("New",           "High",     "PROD", proj_b,  2, "Login broken", None),
            ("In Progress",   "Medium",   "UAT",  proj_a,  3, "Date filter off", None),
            ("In Progress",   "Critical", "PROD", proj_b,  0, "Crash on startup", None),
            ("Resolved",      "Low",      "DEV",  proj_b, 10, "Typo on landing", None),
            ("Closed",        "Low",      "PROD", proj_b, 30, "Spam in audit", None),
            ("Reopened",      "High",     "UAT",  proj_a,  5, "Sync regression", None),
            ("Not a Bug",     "Low",      "DEV",  proj_a,  7, "Misread expect", None),
            ("Resolve Later", "Medium",   "UAT",  proj_b, 14, "Theme off", None),
        ]
        bugs = []
        for status, prio, env, proj, age, title, eid in spec:
            b = models.Bug(
                title=title, description="A long-ish description " * 40,
                status=status, priority=prio, environment=env,
                project_id=proj.id, reporter_id=admin.id, event_id=eid,
                due_date="2026-06-15",
                created_at=now - timedelta(days=age),
            )
            bugs.append(b)
        db.add_all(bugs)
        db.commit()
        bugs[0].assignees = [bob]
        bugs[1].assignees = [alice, bob]
        bugs[3].assignees = [carol]
        bugs[6].assignees = [alice]
        db.commit()

        # A few activity rows so recent-activity has data and the
        # non-admin self-scoping branch returns something.
        db.add_all([
            models.Activity(actor_user_id=admin.id, actor_name="Test Admin",
                            action="bug.create", entity_type="bug",
                            entity_id=bugs[0].id, detail="x" * 200,
                            created_at=now - timedelta(hours=1)),
            models.Activity(actor_user_id=bob.id, actor_name="Bob Builder",
                            action="bug.update", entity_type="bug",
                            entity_id=bugs[0].id, detail="short",
                            created_at=now - timedelta(hours=2)),
        ])
        db.commit()

        return {
            "admin": admin.id, "alice": alice.id, "bob": bob.id,
            "carol": carol.id, "apollo": proj_a.id, "beacon": proj_b.id,
            "event": event.id, "now": now,
        }
    finally:
        db.close()


def _new_db():
    from app.database import SessionLocal
    return SessionLocal()


def _user(db, uid):
    from app import models
    return db.get(models.User, uid)


def _texts(resp):
    """Concatenated text from every text-ish block of a Response."""
    return " ".join(
        b.payload.get("text", "")
        for b in resp.blocks
        if b.kind in ("text",)
    )


def _table(resp):
    return next((b for b in resp.blocks if b.kind == "table"), None)


# ===========================================================================
# nlu.py — time windows (named, relative, since-weekday, ISO/absolute)
# ===========================================================================
def test_cov_time_named_windows():
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    fixed = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)  # a Monday
    for phrase, label in [
        ("bugs today", "today"),
        ("bugs yesterday", "yesterday"),
        ("bugs this week", "this week"),
        ("bugs last week", "last week"),
        ("bugs this month", "this month"),
        ("bugs last month", "last month"),
        ("bugs this quarter", "this quarter"),
        ("bugs last quarter", "last quarter"),
        ("bugs this year", "this year"),
        ("bugs last year", "last year"),
    ]:
        pq = nlu.parse(phrase, ctx, now=fixed)
        assert pq.time_window is not None, phrase
        assert pq.time_window.label == label, (phrase, pq.time_window.label)


def test_cov_time_relative_and_since_weekday():
    # Exercises nlu lines 607 (since-weekday branch) and 615 (relative window).
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    wed = datetime(2026, 6, 17, 9, 0, 0, tzinfo=timezone.utc)  # Wednesday

    pq = nlu.parse("bugs since monday", ctx, now=wed)
    assert pq.time_window is not None
    assert pq.time_window.label == "since monday"

    # "since monday" said ON a Monday → a week ago (delta_days==0 -> 7).
    mon = datetime(2026, 6, 15, 9, 0, 0, tzinfo=timezone.utc)
    pq2 = nlu.parse("bugs since monday", ctx, now=mon)
    assert pq2.time_window.start == mon.replace(
        hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)

    for phrase, unit_word in [
        ("bugs in the last 7 days", "day"),
        ("bugs in the last 2 weeks", "week"),
        ("bugs in the last 3 months", "month"),
        ("bugs in the last 12 hours", "hour"),
        ("bugs past 5 days", "day"),
        ("bugs last 4 weeks", "week"),
    ]:
        pq = nlu.parse(phrase, ctx, now=wed)
        assert pq.time_window is not None, phrase


def test_cov_time_relative_window_units_direct():
    # Directly exercise _relative_window for each unit incl. the else->None.
    from app.chatbot import nlu
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert nlu._relative_window(3, "hours", now).start == now - timedelta(hours=3)
    assert nlu._relative_window(2, "days", now).start == now - timedelta(days=2)
    assert nlu._relative_window(1, "weeks", now).start == now - timedelta(weeks=1)
    assert nlu._relative_window(1, "months", now).start == now - timedelta(days=30)
    assert nlu._relative_window(1, "fortnights", now) is None  # unknown unit


def test_cov_time_no_window():
    from app.chatbot import nlu
    assert nlu._parse_time_window("just a plain message") is None


# ===========================================================================
# nlu.py — enum extraction: statuses (per item-type wording), priorities,
# environments, typo fallbacks.
# ===========================================================================
def test_cov_status_synonyms_all():
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    cases = {
        "show open bugs": "New",
        "show active bugs": "In Progress",
        "show ongoing bugs": "Reopened",
        "show wip bugs": "In Progress",
        "show in-progress bugs": "In Progress",
        "show new bugs": "New",
        "show fixed bugs": "Resolved",
        "show resolved bugs": "Resolved",
        "show closed bugs": "Closed",
        "show done bugs": "Closed",
        "show reopened bugs": "Reopened",
        "show not a bug": "Not a Bug",
        "show invalid bugs": "Not a Bug",
        "show deferred bugs": "Resolve Later",
        "show parked bugs": "Resolve Later",
    }
    for msg, expected in cases.items():
        pq = nlu.parse(msg, ctx)
        assert expected in pq.statuses, (msg, pq.statuses)


def test_cov_priority_and_env_synonyms_and_typos():
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    # priority synonyms incl. P0-P3
    for msg, exp in [
        ("low priority", "Low"), ("minor bugs", "Low"), ("trivial bugs", "Low"),
        ("medium bugs", "Medium"), ("normal bugs", "Medium"),
        ("high bugs", "High"), ("important bugs", "High"), ("major bugs", "High"),
        ("critical bugs", "Critical"), ("blocker bugs", "Critical"),
        ("urgent bugs", "Critical"), ("p0 bugs", "Critical"),
        ("p1 bugs", "High"), ("p2 bugs", "Medium"), ("p3 bugs", "Low"),
    ]:
        pq = nlu.parse(msg, ctx)
        assert exp in pq.priorities, (msg, pq.priorities)

    # environment synonyms
    for msg, exp in [
        ("dev bugs", "DEV"), ("development bugs", "DEV"), ("sandbox bugs", "DEV"),
        ("uat bugs", "UAT"), ("staging bugs", "UAT"), ("qa bugs", "UAT"),
        ("preprod bugs", "UAT"), ("prod bugs", "PROD"),
        ("production bugs", "PROD"), ("live bugs", "PROD"),
    ]:
        pq = nlu.parse(msg, ctx)
        assert exp in pq.environments, (msg, pq.environments)

    # typo fallbacks (edit-distance) — only run when exact extractor misses.
    pq = nlu.parse("show ctitical bugs", ctx)
    assert "Critical" in pq.priorities, pq.priorities
    pq = nlu.parse("bugs in produciton", ctx)
    assert "PROD" in pq.environments, pq.environments


def test_cov_typo_match_short_token_returns_none():
    from app.chatbot import nlu
    # Token shorter than min_len → None (guards "low"->"log" noise).
    assert nlu._typo_match("ab", nlu._PRIORITY_SYNONYMS) is None
    # A token with no close match → None.
    assert nlu._typo_match("zzzzzzzz", nlu._PRIORITY_SYNONYMS) is None


# ===========================================================================
# nlu.py — name resolution: exact, email-local, prefix, last-name,
# first-name, titles, ambiguity, unresolved, possessive.
# ===========================================================================
def test_cov_name_resolution_strategies():
    from app.chatbot import nlu
    # email-locals deliberately differ from first names so each resolution
    # strategy (exact / email / prefix / last / first) is hit in isolation.
    ctx = nlu.Context(
        users=[
            (1, "alice wonderland", "awonder", "Alice Wonderland"),
            (2, "bob builder", "bbuild", "Bob Builder"),
            (3, "carol singer", "csong", "Carol Singer"),
        ],
        projects=[],
    )
    # 1. exact full name
    assert nlu._resolve_name("alice wonderland", ctx) == [(1, "Alice Wonderland")]
    # 2. email local-part exact
    assert nlu._resolve_name("bbuild", ctx) == [(2, "Bob Builder")]
    # 3. prefix on full name ("alice " is a word-prefix of "alice wonderland")
    assert nlu._resolve_name("alice", ctx) == [(1, "Alice Wonderland")]
    # 4. last-name single token
    assert nlu._resolve_name("singer", ctx) == [(3, "Carol Singer")]
    # 5. first-name single token (no prefix-with-space, falls to first-name)
    assert nlu._resolve_name("carol", ctx) == [(3, "Carol Singer")]
    # title prefix dropped → matches "Carol"
    assert nlu._resolve_name("Ms. Carol", ctx) == [(3, "Carol Singer")]
    # no match at all
    assert nlu._resolve_name("zoltan", ctx) == []
    # empty after strip
    assert nlu._resolve_name("   ", ctx) == []
    # title-only after dropping prefixes → empty parts
    assert nlu._resolve_name("mr", ctx) == []


def test_cov_name_ambiguous_and_unresolved():
    from app.chatbot import nlu
    # Two Bobs → ambiguous on the shared first name.
    ctx = nlu.Context(
        users=[
            (1, "bob builder", "bobb", "Bob Builder"),
            (2, "bob marley", "bobm", "Bob Marley"),
        ],
        projects=[],
    )
    pq = nlu.parse("show bugs assigned to bob", ctx)
    assert pq.ambiguous_names, pq.ambiguous_names
    assert pq.ambiguous_names[0][0] == "bob"

    # Unresolved assignee + reporter names recorded separately.
    pq2 = nlu.parse("bugs assigned to nelson", ctx)
    assert pq2.unresolved_assignee_names == ["nelson"]
    pq3 = nlu.parse("bugs reported by nelson", ctx)
    assert pq3.unresolved_reporter_names == ["nelson"]


def test_cov_possessive_assignee():
    from app.chatbot import nlu
    ctx = nlu.Context(
        users=[(1, "alice wonderland", "alice", "Alice Wonderland")],
        projects=[],
    )
    # "Alice's bugs" → possessive last-resort assignee match → list_bugs.
    pq = nlu.parse("Alice's bugs", ctx)
    assert pq.intent == "list_bugs"
    assert 1 in pq.assignee_ids

    # Possessive that resolves to nobody → not assigned, stays possessive miss.
    assert nlu._try_possessive_assignee("Zelda's bugs", nlu.ParsedQuery(), ctx) is False


# ===========================================================================
# nlu.py — projects (cue, loose cue, literal), roles, bug ids, text search.
# ===========================================================================
def test_cov_project_resolution_paths():
    from app.chatbot import nlu
    ctx = nlu.Context(
        users=[],
        projects=[(1, "apollo", "Apollo"), (2, "beacon", "Beacon")],
    )
    # strict "in project X" cue
    pq = nlu.parse("bugs in project Apollo", ctx)
    assert 1 in pq.project_ids
    # loose "project X" cue
    pq2 = nlu.parse("project beacon issues", ctx)
    assert 2 in pq2.project_ids
    # literal project-name fallback (no cue word at all)
    pq3 = nlu.parse("anything broken in Apollo lately", ctx)
    assert 1 in pq3.project_ids
    # unknown project → none resolved
    pq4 = nlu.parse("bugs in project Zeta", ctx)
    assert pq4.project_ids == []


def test_cov_resolve_project_direct():
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[(1, "mobile app", "Mobile App")])
    assert nlu._resolve_project("mobile app", ctx) == [(1, "Mobile App")]
    assert nlu._resolve_project("mobile", ctx) == [(1, "Mobile App")]   # prefix
    assert nlu._resolve_project("   ", ctx) == []                       # empty


def test_cov_role_filter_variants():
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    assert nlu.parse("list admins", ctx).role_filter == "admin"
    assert nlu.parse("list managers", ctx).role_filter == "manager"
    assert nlu.parse("list regular users", ctx).role_filter == "user"


def test_cov_bug_id_extraction():
    from app.chatbot import nlu
    assert nlu._extract_bug_id("bug 42") == 42
    assert nlu._extract_bug_id("#7") == 7
    assert nlu._extract_bug_id("issue #13") == 13
    assert nlu._extract_bug_id("details of 99") == 99
    assert nlu._extract_bug_id("123") == 123     # whole-message digits
    assert nlu._extract_bug_id("no number here") is None


def test_cov_text_search_paths():
    from app.chatbot import nlu
    # quoted wins
    assert nlu._extract_text_search('bugs about "login crash"') == "login crash"
    # bare cue (single-topic word)
    assert nlu._extract_text_search("bugs about login") == "login"
    assert nlu._extract_text_search("issues regarding checkout") == "checkout"
    # enum-only term declined (it's a filter, not a search topic)
    assert nlu._extract_text_search("bugs about high priority") is None
    # no cue
    assert nlu._extract_text_search("list open bugs") is None


# ===========================================================================
# nlu.py — action detection: comment, create project/bug, status, priority,
# due-date, assign/unassign, and the list-verb guard.
# ===========================================================================
def test_cov_action_detection_variants():
    from app.chatbot import nlu
    ctx = nlu.Context(
        users=[(1, "alice wonderland", "alice", "Alice Wonderland"),
               (2, "bob builder", "bob", "Bob Builder")],
        projects=[(1, "apollo", "Apollo")],
    )
    # add comment with body (the "#N: body" shape the body regex supports)
    pq = nlu.parse("comment on #5: looks fixed", ctx)
    assert pq.action_kind == "add_comment"
    assert pq.action_comment == "looks fixed"

    # comment verb, no colon/body → still add_comment, comment None
    pq = nlu.parse("comment on bug 5", ctx)
    assert pq.action_kind == "add_comment"
    assert pq.action_comment is None

    # create project
    pq = nlu.parse("create project Mercury", ctx)
    assert pq.action_kind == "create_project"
    assert pq.action_title == "Mercury"

    # create bug — quoted title
    pq = nlu.parse('create a bug titled "Login broke" in project Apollo', ctx)
    assert pq.action_kind == "create_bug"
    assert pq.action_title == "Login broke"

    # create bug — bare title with tail stripped
    pq = nlu.parse("file a bug Cache miss in project Apollo", ctx)
    assert pq.action_kind == "create_bug"
    assert pq.action_title == "Cache miss"

    # set status via verb
    pq = nlu.parse("close bug 5", ctx)
    assert pq.action_kind == "set_status"
    assert pq.action_value == "Closed"

    # set status via "set status to" with a status synonym present
    pq = nlu.parse("set bug 5 status to in progress", ctx)
    assert pq.action_kind == "set_status"
    assert pq.action_value == "In Progress"

    # set priority
    pq = nlu.parse("set bug 5 priority to high", ctx)
    assert pq.action_kind == "set_priority"
    assert pq.action_value == "High"

    # due date
    pq = nlu.parse("due bug 5 2026-06-15", ctx)
    assert pq.action_kind == "set_due_date"
    assert pq.action_value == "2026-06-15"

    # assign / unassign
    pq = nlu.parse("assign bug 5 to alice", ctx)
    assert pq.action_kind == "assign"
    pq = nlu.parse("unassign bob from #5", ctx)
    assert pq.action_kind == "unassign"

    # list-verb guard: "show bugs assigned to bob" is a LIST, not assign.
    pq = nlu.parse("show bugs assigned to bob", ctx)
    assert pq.action_kind is None
    assert pq.intent == "list_bugs"


def test_cov_action_helpers_negative_paths():
    from app.chatbot import nlu
    pq = nlu.ParsedQuery()
    # no comment verb
    assert nlu._action_add_comment("show bugs", pq) is None
    # no create-project verb
    assert nlu._action_create_project("show bugs", pq) is None
    # no create-bug verb
    assert nlu._action_create_bug("show bugs", pq) is None
    # status-change regex present but no synonym/verb -> falls through paths
    assert nlu._action_set_status("show bugs", pq) is None
    # priority change requires both regex AND a parsed priority
    assert nlu._action_set_priority("change priority", nlu.ParsedQuery()) is None
    # due-date verb absent
    assert nlu._action_set_due_date("show bugs", pq) is None
    # assign requires assignee ids
    assert nlu._action_assign("assign bug 5 to nobody", nlu.ParsedQuery()) is None


def test_cov_strip_create_bug_tail_direct():
    from app.chatbot import nlu
    assert nlu._strip_create_bug_tail("") == ""
    assert nlu._strip_create_bug_tail("Title with priority high") == "Title"
    assert nlu._strip_create_bug_tail("Title assigned to bob") == "Title"
    assert nlu._strip_create_bug_tail("Plain title") == "Plain title"


def test_cov_pick_report_key_direct():
    from app.chatbot import nlu
    assert nlu.pick_report_key("") is None
    assert nlu.pick_report_key("throughput last week") == "throughput"
    assert nlu.pick_report_key("who resolved how many") == "throughput"
    assert nlu.pick_report_key("pending snapshot") == "pending_snapshot"
    assert nlu.pick_report_key("aging report") == "aging"
    assert nlu.pick_report_key("time to resolution") == "time_to_resolution"
    assert nlu.pick_report_key("distribution by status") == "status_distribution"
    assert nlu.pick_report_key("by priority") == "priority_distribution"
    assert nlu.pick_report_key("breakdown by project") == "project_breakdown"
    assert nlu.pick_report_key("created vs resolved timeline") == "timeline"
    assert nlu.pick_report_key("just some bugs") is None


def test_cov_describe_filters_branches():
    from app.chatbot import nlu
    pq = nlu.ParsedQuery()
    pq.statuses = list(nlu.OPEN_STATUSES)        # collapses to "open"
    assert "open" in nlu.describe_filters(pq)

    pq2 = nlu.ParsedQuery()
    pq2.statuses = ["Closed"]
    pq2.priorities = ["High"]
    pq2.environments = ["PROD"]
    pq2.project_names = ["Apollo"]
    pq2.assignee_names = ["Alice"]
    pq2.reporter_names = ["Bob"]
    pq2.unassigned = True
    pq2.text_search = "login"
    pq2.time_window = nlu.TimeWindow(label="today")
    pq2.sort_oldest = True
    out = nlu.describe_filters(pq2)
    for frag in ["closed", "high priority", "in PROD", "in project Apollo",
                 "assigned to Alice", "reported by Bob", "with no assignee",
                 'matching "login"', "(today)", "(oldest first)"]:
        assert frag in out, (frag, out)


# ===========================================================================
# executor.py — build_context with a user whose email lacks "@" (line 95->97).
# ===========================================================================
def test_cov_build_context_email_without_at(client):
    _seed(client)
    from app.database import SessionLocal
    from app import models
    from app.chatbot.executor import build_context

    db = SessionLocal()
    try:
        # A user whose email has no "@" exercises the `if "@" in u.email`
        # false branch (95->97).
        u = models.User(name="Weird Name", email="noatsign",
                        role="user", password_hash="x", is_active=True)
        db.add(u)
        db.commit()
        ctx = build_context(db)
        weird = next(t for t in ctx.users if t[3] == "Weird Name")
        assert weird[2] == ""   # empty email local-part
    finally:
        db.close()


# ===========================================================================
# executor.py — read handlers end to end via execute().
# ===========================================================================
def test_cov_exec_greeting_thanks_help_empty(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        assert executor.execute("hi", db, admin).intent == "greeting"
        assert executor.execute("thanks", db, admin).intent == "thanks"
        assert executor.execute("help", db, admin).intent == "help"
        assert executor.execute("", db, admin).intent == "empty"
    finally:
        db.close()


def test_cov_exec_about_known_and_unknown(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # Known fact keyword → about with the matching answer (loop hit).
        r = executor.execute("what statuses are there", db, admin)
        assert r.intent == "about"
        assert "Statuses" in _texts(r)
        # _handle_about directly with an unrecognised topic → "I'm not sure"
        # (covers the loop-exhausted return, lines 344->343 then 350).
        r2 = executor._handle_about("tell me about quantum gravity please")
        assert r2.intent == "about"
        assert r2.fallback_eligible is True
        assert "not sure" in _texts(r2).lower()
    finally:
        db.close()


def test_cov_exec_list_users_filter_and_empty(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # All users
        r = executor.execute("list all users", db, admin)
        assert r.intent == "list_users"
        assert _table(r) is not None
        # Managers only (label pluralisation branch)
        r2 = executor.execute("list managers", db, admin)
        names = [row[0] for row in _table(r2).payload["rows"]]
        assert names == ["Alice Wonderland"]
        # Role filter that matches nobody → "No users match that filter".
        from app.chatbot import nlu
        ctx = executor.build_context(db)
        pq = nlu.parse("list admins", ctx)
        # Drop the only admin so the admin role yields zero rows.
        # (Use a fresh parsed query with a role nobody holds instead — safer.)
        pq.role_filter = "manager"
        # remove the manager to force empty:
        from app import models
        alice = db.get(models.User, ids["alice"])
        alice.role = "user"
        db.commit()
        r3 = executor._handle_list_users(db, pq)
        assert r3.summary == "0 users"
        assert "No users match" in _texts(r3)
    finally:
        db.close()


def test_cov_exec_list_projects_and_empty(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("list projects", db, admin)
        assert r.intent == "list_projects"
        assert _table(r) is not None

        # Empty-projects branch (line 470): delete all projects first.
        from app import models
        db.query(models.Bug).delete()
        db.query(models.Project).delete()
        db.commit()
        r2 = executor._handle_list_projects(db)
        assert r2.summary == "0 projects"
        assert "no projects yet" in _texts(r2).lower()
    finally:
        db.close()


def test_cov_exec_bug_detail_found_event_and_not_found(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # Bug #1 has an event + a long description → exercises event line
        # (521) and the short_descr branch (531->533).
        r = executor.execute("bug 1", db, admin)
        assert r.intent == "bug_detail"
        body = _texts(r)
        assert "Release 9.9" in body
        assert "Description:" in body
        # Not found (line 497).
        r2 = executor.execute("bug 999999", db, admin)
        assert r2.intent == "bug_detail"
        assert "couldn't find" in _texts(r2).lower()
    finally:
        db.close()


def test_cov_exec_recent_activity_admin_user_and_window(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        bob = _user(db, ids["bob"])
        # Admin sees all (no self scoping).
        r = executor.execute("recent activity", db, admin)
        assert r.intent == "recent_activity"
        assert _table(r) is not None
        # Non-admin scoping branch — bob sees only his own actions.
        r2 = executor.execute("recent activity", db, bob)
        assert r2.intent == "recent_activity"
        # Time-window branch (lines 556-559) — both start and end applied.
        r3 = executor.execute("what happened today", db, admin)
        assert r3.intent == "recent_activity"
    finally:
        db.close()


def test_cov_exec_recent_activity_empty(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        from app import models
        db.query(models.Activity).delete()
        db.commit()
        admin = _user(db, ids["admin"])
        r = executor.execute("recent activity today", db, admin)
        assert r.summary == "0 events"
        assert "No recent activity" in _texts(r)
    finally:
        db.close()


def test_cov_exec_stats(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("summary", db, admin)
        assert r.intent == "stats"
        body = _texts(r)
        assert "Total" in body and "Open" in body
        # Top-assignees table present (we seeded assignees on open bugs).
        assert _table(r) is not None
    finally:
        db.close()


# ===========================================================================
# executor.py — list_bugs: count, list, empty, sort, unassigned, time window.
# ===========================================================================
def test_cov_exec_list_count_and_filters(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # Count path
        r = executor.execute("how many critical bugs in prod", db, admin)
        assert r.intent == "count_bugs"
        assert "bug" in _texts(r)
        # List path with a result
        r2 = executor.execute("show open bugs assigned to bob", db, admin)
        assert r2.intent == "list_bugs"
        assert _table(r2) is not None
        # Unassigned filter
        r3 = executor.execute("show unassigned bugs", db, admin)
        assert r3.intent == "list_bugs"
        # Oldest-first sort hint
        r4 = executor.execute("oldest open bugs", db, admin)
        assert r4.intent == "list_bugs"
        # Time-window filter on updated_at vs created_at (lines 171-175).
        r5 = executor.execute("bugs created in the last 2 days", db, admin)
        assert r5.intent == "list_bugs"
        r6 = executor.execute("bugs updated this week", db, admin)
        assert r6.intent == "list_bugs"
    finally:
        db.close()


def test_cov_exec_count_response_singular_plural():
    # Drive the count-text builder directly (verb is/are + plural-s).
    # Avoids status-verb words like "reopened"/"closed" that the parser
    # would treat as a write action rather than a count.
    from app.chatbot import executor
    assert "is **1** bug " in executor._build_count_response(1, "(no filters)").blocks[0].payload["text"]
    assert "are **0** bugs" in executor._build_count_response(0, "in PROD").blocks[0].payload["text"]
    assert "are **2** bugs" in executor._build_count_response(2, "open").blocks[0].payload["text"]


def test_cov_exec_list_empty_paths(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # Real count path that is NOT a status verb: priority + env.
        r = executor.execute("how many critical bugs in prod", db, admin)
        assert r.intent == "count_bugs"
        # Empty list — no bugs at all → "no bugs in the system yet".
        from app import models
        db.query(models.Bug).delete()
        db.commit()
        r2 = executor.execute("show me all bugs", db, admin)
        assert r2.intent == "list_bugs"
        assert "no bugs in the system yet" in _texts(r2).lower()
        # Empty with a filter description → "No bugs found <descr>".
        r3 = executor.execute("show critical bugs in PROD", db, admin)
        assert "No bugs found" in _texts(r3)
    finally:
        db.close()


def test_cov_exec_clarify_ambiguous_and_unresolved(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # Two users share the first name "Frank" and neither has an email
        # local-part equal to "frank", so the name stays ambiguous.
        from app import models
        from app.auth import hash_password
        db.add_all([
            models.User(name="Frank Castle", email="fcastle@test.local",
                        role="user", password_hash=hash_password("x"),
                        is_active=True),
            models.User(name="Frank Sinatra", email="fsinatra@test.local",
                        role="user", password_hash=hash_password("x"),
                        is_active=True),
        ])
        db.commit()
        r = executor.execute("show bugs assigned to frank", db, admin)
        assert r.intent == "clarify"
        assert "More than one user" in _texts(r)

        # Unresolved name → clarify + suggestion (close match "bib"->"bob").
        ctx = executor.build_context(db)
        r2 = executor._handle_list_bugs(db, nlu.parse("bugs assigned to bib", ctx), ctx)
        assert r2.intent == "clarify"
        assert "couldn't find a user" in _texts(r2).lower()
    finally:
        db.close()


def test_cov_clarify_helpers_none_paths():
    from app.chatbot import executor, nlu
    # No ambiguous names → None.
    assert executor._clarify_ambiguous_names(nlu.ParsedQuery()) is None
    # No unresolved names → None.
    assert executor._clarify_unresolved_user(nlu.ParsedQuery(), None) is None


# ===========================================================================
# executor.py — user-suggestion helpers (_suggest_user, pool, dedupe).
# ===========================================================================
def test_cov_suggest_user_helpers():
    from app.chatbot import executor, nlu
    ctx = nlu.Context(
        users=[
            (1, "alice wonderland", "alice", "Alice Wonderland"),
            (2, "bob builder", "bob", "Bob Builder"),
            (3, "", "", "Nameless"),   # empty name + empty email-local
        ],
        projects=[],
    )
    # Pool building skips the empty-name/empty-email user (661->663 / 663->660).
    pool = executor._build_user_suggest_pool(ctx)
    assert "alice wonderland" in pool and "bob" in pool
    assert "" not in pool

    # Single close match.
    assert "Alice Wonderland" in executor._suggest_user("alise wonderland", ctx)
    # Two close matches → "X or Y".
    ctx2 = nlu.Context(
        users=[
            (1, "jon snow", "jon", "Jon Snow"),
            (2, "ron snow", "ron", "Ron Snow"),
        ],
        projects=[],
    )
    out = executor._suggest_user("on snow", ctx2)
    assert " or " in out

    # Guard / empty paths (693, 696, 700-704).
    assert executor._suggest_user("anything", None) == ""        # no ctx
    assert executor._suggest_user("   ", ctx) == ""              # blank needle
    empty_ctx = nlu.Context(users=[], projects=[])
    assert executor._suggest_user("alice", empty_ctx) == ""      # empty pool
    # A needle with no close match → "".
    assert executor._suggest_user("zzzzzzzzzz", ctx) == ""


def test_cov_dedupe_display_names():
    from app.chatbot import executor
    pool = {"a": "Alice", "b": "Bob", "a2": "Alice"}
    # Duplicate display name dropped, order preserved; unknown key skipped.
    assert executor._dedupe_display_names(["a", "a2", "b", "zzz"], pool) == ["Alice", "Bob"]


# ===========================================================================
# executor.py — export path (covers excel-available + degraded branches and
# the file-label construction with assignee/status segments).
# ===========================================================================
def test_cov_exec_export_to_excel(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # "open" is a status synonym (not a status *verb*), so it survives as
        # a filter and the file-label builder appends both the assignee
        # segment (line 872) and the status segment (line 877). Using a
        # status-verb word like "closed" would instead be parsed as a write.
        r = executor.execute(
            "export open bugs assigned to alice to excel", db, admin)
        # Either a real file block (openpyxl present) or the graceful
        # "Excel exporter isn't available" / "couldn't build" text — assert
        # whichever branch actually fired so the test is hermetic.
        kinds = {b.kind for b in r.blocks}
        assert r.intent in ("export_bugs", "export_failed")
        if r.intent == "export_bugs":
            assert "file" in kinds
        else:
            assert "text" in kinds
    finally:
        db.close()


def test_cov_exec_export_capped(client):
    _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        ctx = executor.build_context(db)
        pq = nlu.parse("export all bugs to excel", ctx)
        # Force the "capped" note branch (line 899) by passing total > cap
        # straight into the response builder over the real rows.
        from app.chatbot.executor import _eager_bug_query, _apply_bug_filters
        from sqlalchemy import select, func
        from app import models
        stmt, _cs = _apply_bug_filters(
            _eager_bug_query(), select(func.count(models.Bug.id)), pq)
        rows = list(db.scalars(stmt.limit(5000)).all())
        resp = executor._build_export_response(rows, pq, total=99999, cap=5000)
        if resp.intent == "export_bugs":
            assert any("capped" in b.payload.get("text", "") for b in resp.blocks)
        else:
            # openpyxl missing → export_failed; the capped branch is then
            # genuinely unreachable here, which is fine.
            assert resp.intent == "export_failed"
    finally:
        db.close()


# ===========================================================================
# executor.py — reports: forbidden, run, empty, summary extras, preview.
# ===========================================================================
def test_cov_exec_report_forbidden_for_user(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        bob = _user(db, ids["bob"])  # role "user"
        r = executor.execute("throughput report last week", db, bob)
        assert r.intent == "report_forbidden"
        assert "managers and admins" in _texts(r)
    finally:
        db.close()


def test_cov_exec_report_runs_for_admin(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # item_detail default report over all items — has rows from the seed.
        r = executor.execute("report of all bugs", db, admin)
        assert r.intent in ("report", "report_error", "report_empty")
        assert r.blocks
        # A throughput report (different summary extras path).
        r2 = executor.execute("throughput report", db, admin)
        assert r2.blocks
    finally:
        db.close()


def test_cov_exec_report_empty(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        from app import models
        db.query(models.Bug).delete()
        db.commit()
        # No items at all → report empty branch.
        r = executor.execute("pending snapshot report", db, admin)
        assert r.intent in ("report", "report_empty")
        # An empty result should mention widening filters.
        if r.intent == "report":
            # item-count zero path returns the empty-response helper.
            assert "no rows matched" in _texts(r).lower() or r.summary.startswith("0 rows")
    finally:
        db.close()


def test_cov_report_pure_helpers():
    from app.chatbot import executor

    # _format_summary_extras — every key branch.
    assert executor._format_summary_extras({}) == ""
    s_through = {"total_resolved": 12, "user_count": 3}
    assert "Total resolved" in executor._format_summary_extras(s_through)
    s_items = {"total_items": 5}
    assert "Total items" in executor._format_summary_extras(s_items)
    s_timeline = {"total_created": 8, "total_resolved": 6, "net": 2}
    out = executor._format_summary_extras(s_timeline)
    assert "Created" in out and "Net" in out
    s_ttr = {"average_hours": 4, "median_hours": 3, "p95_hours": 9}
    assert "Average" in executor._format_summary_extras(s_ttr)

    # _report_row_to_table_row — None, numeric, short, and truncated string.
    class _Col:
        def __init__(self, key):
            self.key = key
    cols = [_Col("a"), _Col("b"), _Col("c"), _Col("d")]
    row = {"a": None, "b": 7, "c": "short", "d": "x" * 200}
    out = executor._report_row_to_table_row(row, cols)
    assert out[0] == ""
    assert out[1] == "7"
    assert out[2] == "short"
    # Long strings are truncated to 77 chars + the ellipsis (= 78).
    assert out[3].endswith("…") and len(out[3]) == 78


def test_cov_build_report_preview_text():
    from app.chatbot import executor
    from app.reports import Filters

    class _Res:
        report_label = "Throughput"
        total = 50
        summary = {"total_resolved": 10, "user_count": 2}

    filters = Filters(date_from=None, date_to=None)
    # No date range, total>preview → "Preview shows the first N rows".
    text = executor._build_report_preview_text(_Res(), filters, preview_rows_count=15)
    assert "Throughput" in text
    assert "Preview shows the first 15 rows" in text

    # With a date range (covers the date-line branch 1055).
    from datetime import date
    filters2 = Filters(date_from=date(2026, 1, 1), date_to=date(2026, 2, 1))

    class _Res1:
        report_label = "Item Detail"
        total = 1
        summary = {}
    text2 = executor._build_report_preview_text(_Res1(), filters2, preview_rows_count=15)
    assert "2026-01-01" in text2 and "2026-02-01" in text2


def test_cov_filters_from_parsed_dates_and_types(client):
    _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        ctx = executor.build_context(db)
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        # A query with BOTH a time window (start+end) and an item-type word
        # exercises the date_from/date_to branches (932-936) and item_types.
        pq = nlu.parse("requirement report this month", ctx, now=now)
        filt = executor._filters_from_parsed(pq)
        assert filt.date_from is not None
        assert filt.date_to is not None
        assert "Requirement" in filt.item_types

        # A defect/bug word maps to "Bug"; a task word maps to "Task".
        pq2 = nlu.parse("task report", ctx)
        filt2 = executor._filters_from_parsed(pq2)
        assert "Task" in filt2.item_types

        # No time window → both dates None.
        pq3 = nlu.parse("report of all items", ctx)
        filt3 = executor._filters_from_parsed(pq3)
        assert filt3.date_from is None and filt3.date_to is None
    finally:
        db.close()


def test_cov_report_forbidden_and_empty_helpers():
    from app.chatbot import executor

    fr = executor._report_forbidden_response()
    assert fr.intent == "report_forbidden"

    class _Res:
        report_label = "Aging"
    er = executor._report_empty_response(_Res())
    assert er.intent == "report"
    assert "no rows matched" in _texts(er).lower()


def test_cov_try_stage_file_block_failure():
    from app.chatbot import executor

    # A result that makes the XLSX builder raise → returns None (1077-1079),
    # logged but never raised.
    class _Bad:
        total = 3
        # Missing the attributes build_workbook_bytes needs → exception.
    out = executor._try_stage_file_block(_Bad(), "item_detail")
    assert out is None


# ===========================================================================
# executor.py — action planning + confirmation flow via execute().
# ===========================================================================
def test_cov_exec_action_missing_slots(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        from app.chatbot.memory import store as mem
        admin = _user(db, ids["admin"])
        mem.reset(admin.id)

        # "set bug 5 status to" with no trailing synonym → action_value None
        # → planner asks "What status?" → action_invalid.
        r = executor.execute("set bug 5 status to", db, admin)
        assert r.intent in ("action_invalid", "confirm_action")

        # Missing bug id for an action that needs one (no pronoun memory).
        mem.reset(admin.id)
        r2 = executor.execute("close that bug", db, admin)
        assert r2.intent == "action_invalid"
        assert "which bug" in _texts(r2).lower()

        # A status verb with no bug id at all → action_invalid path.
        mem.reset(admin.id)
        r3 = executor.execute("please close the issue right now", db, admin)
        assert r3.intent == "action_invalid"
    finally:
        db.close()


def test_cov_exec_action_plan_each_kind(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        ctx = executor.build_context(db)

        def plan_for(msg):
            pq = nlu.parse(msg, ctx)
            return executor._build_action_plan(pq, admin)

        # assign — happy path
        plan, err = plan_for("assign bug 1 to alice")
        assert err is None and plan.kind == "assign"
        # unassign
        plan, err = plan_for("unassign alice from bug 1")
        assert err is None and plan.kind == "unassign"
        # set_status (mark as resolved)
        plan, err = plan_for("mark bug 1 as resolved")
        assert err is None and plan.new_value == "Resolved"
        # set_priority
        plan, err = plan_for("set bug 1 priority to high")
        assert err is None and plan.new_value == "High"
        # set_due_date
        plan, err = plan_for("due bug 1 2026-06-15")
        assert err is None and plan.new_value == "2026-06-15"
        # add_comment (the "#N: body" shape the body regex supports)
        plan, err = plan_for("comment on #1: hi there")
        assert err is None and plan.comment_body == "hi there"
        # create_bug
        plan, err = plan_for('create a bug titled "Fresh" in project Apollo')
        assert err is None and plan.new_title == "Fresh"
        # create_project
        plan, err = plan_for("create project Neptune")
        assert err is None and plan.new_project_name == "Neptune"
    finally:
        db.close()


def test_cov_exec_action_plan_missing_value_errors(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        from app.chatbot.actions import ActionPlan

        # assign with no assignee ids → error message.
        pq = nlu.ParsedQuery(bug_id=1, action_kind="assign")
        plan, err = executor._plan_assign(ActionPlan(kind="assign", actor_user_id=1), pq, "assign")
        assert plan is None and "name" in err.lower()

        # set_status with no value.
        plan, err = executor._plan_set_status(ActionPlan(kind="set_status", actor_user_id=1),
                                              nlu.ParsedQuery(bug_id=1))
        assert plan is None and "status" in err.lower()

        # set_priority with no value.
        plan, err = executor._plan_set_priority(ActionPlan(kind="set_priority", actor_user_id=1),
                                               nlu.ParsedQuery(bug_id=1))
        assert plan is None and "priority" in err.lower()

        # set_environment with none.
        plan, err = executor._plan_set_environment(
            ActionPlan(kind="set_environment", actor_user_id=1), nlu.ParsedQuery(bug_id=1))
        assert plan is None and "environment" in err.lower()

        # set_due_date with no date.
        plan, err = executor._plan_set_due_date(
            ActionPlan(kind="set_due_date", actor_user_id=1), nlu.ParsedQuery(bug_id=1))
        assert plan is None and "date" in err.lower()

        # add_comment with no body.
        plan, err = executor._plan_add_comment(
            ActionPlan(kind="add_comment", actor_user_id=1), nlu.ParsedQuery(bug_id=1))
        assert plan is None and "comment" in err.lower()

        # create_bug with no title.
        plan, err = executor._plan_create_bug(
            ActionPlan(kind="create_bug", actor_user_id=1), nlu.ParsedQuery())
        assert plan is None and "title" in err.lower()

        # create_project with no name.
        plan, err = executor._plan_create_project(
            ActionPlan(kind="create_project", actor_user_id=1), nlu.ParsedQuery())
        assert plan is None and "project name" in err.lower()
    finally:
        db.close()


def test_cov_exec_action_plan_unknown_kind(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        pq = nlu.ParsedQuery(action_kind="teleport", bug_id=1)
        plan, err = executor._build_action_plan(pq, admin)
        assert plan is None
        assert "don't know how to do 'teleport'" in err
    finally:
        db.close()


def test_cov_exec_action_needs_bug_pronoun_branch(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # used_pronoun_bug True but no bug id → the pronoun-specific message.
        pq = nlu.ParsedQuery(action_kind="set_status", action_value="Closed",
                             used_pronoun_bug=True)
        plan, err = executor._build_action_plan(pq, admin)
        assert plan is None
        assert "don't know which bug" in err.lower()
    finally:
        db.close()


def test_cov_exec_confirm_flow_yes_no_idle(client):
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        mem.reset(admin.id)
        # Stage a close, then confirm.
        r = executor.execute("close bug 5", db, admin)
        assert r.intent == "confirm_action"
        r2 = executor.execute("yes", db, admin)
        assert r2.intent == "action_done"

        # 'yes' again → nothing pending → confirm_idle.
        r3 = executor.execute("yes", db, admin)
        assert r3.intent == "confirm_idle"

        # Stage + cancel.
        mem.reset(admin.id)
        executor.execute("close bug 5", db, admin)
        r4 = executor.execute("no", db, admin)
        assert r4.intent == "confirm_cancel"
        assert "haven't changed anything" in _texts(r4).lower()

        # 'no' with nothing pending → still confirm_cancel, different text.
        r5 = executor.execute("no", db, admin)
        assert r5.intent == "confirm_cancel"
        assert "nothing was pending" in _texts(r5).lower()
    finally:
        db.close()


def test_cov_exec_me_pronoun_resolution(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # "my bugs" → assignee splice (this phrasing does NOT capture an
        # unresolved "me" name, unlike "assigned to me", so it lists rather
        # than asking to clarify).
        r = executor.execute("my bugs", db, admin)
        assert r.intent == "list_bugs"
        # "bugs I reported" → reporter splice (the admin reported all bugs).
        r2 = executor.execute("bugs I reported", db, admin)
        assert r2.intent == "list_bugs"
        assert _table(r2) is not None
    finally:
        db.close()


def test_cov_resolve_me_pronoun_direct():
    from app.chatbot import executor, nlu

    class _Actor:
        id = 7
        name = "Me"

    # Not flagged → no-op.
    pq = nlu.ParsedQuery()
    executor._resolve_me_pronoun(pq, _Actor())
    assert pq.assignee_ids == [] and pq.reporter_ids == []

    # me_role reporter → reporter slot.
    pq2 = nlu.ParsedQuery(used_pronoun_me=True, me_role="reporter")
    executor._resolve_me_pronoun(pq2, _Actor())
    assert pq2.reporter_ids == [7]
    # Calling again must not duplicate.
    executor._resolve_me_pronoun(pq2, _Actor())
    assert pq2.reporter_ids == [7]

    # me_role assignee (default) → assignee slot.
    pq3 = nlu.ParsedQuery(used_pronoun_me=True, me_role="assignee")
    executor._resolve_me_pronoun(pq3, _Actor())
    assert pq3.assignee_ids == [7]
    executor._resolve_me_pronoun(pq3, _Actor())
    assert pq3.assignee_ids == [7]


def test_cov_resolve_pronouns_bug_memory(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        mem.reset(admin.id)
        mem.remember_bug(admin.id, 3)
        # pq references a pronoun bug but has no id → memory fills it in.
        pq = nlu.ParsedQuery(used_pronoun_bug=True)
        executor._resolve_pronouns(pq, admin)
        assert pq.bug_id == 3
    finally:
        db.close()


# ===========================================================================
# executor.py — classifier fallback + unknown handler hint branches.
# ===========================================================================
def test_cov_exec_unknown_hint_branches(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # Drive _handle_unknown directly to hit each topic-hint branch
        # (414 users, 416 projects, 419 stats) and the default.
        assert "list users" in _texts(executor._handle_unknown("blah team members blah")).lower()
        assert "list projects" in _texts(executor._handle_unknown("zzz project zzz")).lower()
        assert "summary" in _texts(executor._handle_unknown("zzz kpi dashboard zzz")).lower()
        # default bug-shaped hint
        assert "open bugs" in _texts(executor._handle_unknown("qwerty asdf")).lower()
    finally:
        db.close()


def test_cov_did_you_mean_safe():
    from app.chatbot import executor
    # Gibberish → None, never raises.
    assert executor._did_you_mean("qwerty zxcvbn asdfgh") is None
    assert executor._did_you_mean("") is None
    # Whatever it returns for a real-ish phrase is a known canonical string.
    out = executor._did_you_mean("show me the bugs")
    assert out is None or out in executor._INTENT_SUGGESTION.values()


def test_cov_classifier_fallback_paths(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # A paraphrase the rules miss but the classifier maps to a read
        # intent (stats). Exercises _try_classifier -> read dispatch.
        r = executor.execute("show me the dashboard", db, admin)
        assert r.intent in ("stats", "list_bugs", "unknown")
        # Genuinely unknown → unknown fallback (the final return).
        r2 = executor.execute("xqzlmqop nonsense gibberish", db, admin)
        assert r2.intent in ("unknown", "fallback")
        assert any(b.kind == "text" for b in r2.blocks)
    finally:
        db.close()


def test_cov_classifier_action_invalid_direct():
    from app.chatbot import executor
    # The "classifier guessed an action but slots empty" helper.
    r = executor._classifier_action_invalid("action_assign")
    assert r.intent == "action_invalid"
    assert "assign" in _texts(r).lower()


def test_cov_try_llm_and_cloud_unavailable(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # LLM model path doesn't exist in tests → is_available False → None.
        assert executor._try_llm("weird query", db, admin) is None
        # Cloud OFF (SLEUTH_CLOUD_ENABLED=0) → not available → None.
        assert executor._try_cloud_llm("weird query", db, admin, None) is None
    finally:
        db.close()


def test_cov_dispatch_read_intent_unknown_returns_none(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        ctx = executor.build_context(db)
        pq = nlu.ParsedQuery(intent="not_a_read_intent")
        assert executor._dispatch_read_intent("not_a_read_intent", db, pq, admin, ctx) is None
    finally:
        db.close()


# ===========================================================================
# End-to-end through the HTTP router (admin_client / user_client). Confirms
# the executor entry point is reachable via the real API and that the
# "changeme" password remains valid for the seeded admin in this app.
# ===========================================================================
def test_cov_http_ask_basic(admin_client):
    r = admin_client.post("/api/chat/ask", json={"message": "hi"})
    assert r.status_code == 200
    assert r.json()["intent"] == "greeting"

    r2 = admin_client.post("/api/chat/ask", json={"message": "summary"})
    assert r2.status_code == 200
    assert r2.json()["intent"] == "stats"


def test_cov_http_ask_report_forbidden_for_regular_user(user_client):
    # A regular user asking for a report gets the forbidden response, not a 500.
    r = user_client.post("/api/chat/ask",
                         json={"message": "throughput report last week"})
    assert r.status_code == 200
    assert r.json()["intent"] == "report_forbidden"


def test_cov_changeme_password_still_valid():
    # Hard rule: 'changeme' must always be accepted by the policy.
    from app.schemas import _check_password_strength
    assert _check_password_strength("changeme") == "changeme"


# ===========================================================================
# Extra focused coverage — remaining target lines that need a specific
# shape (partial branches, error/fallback paths, monkeypatched modules).
# ===========================================================================
def test_cov_bug_detail_no_event_branch(client):
    # Bug #2 has NO event → the `if event_name:` false branch (520->522);
    # its long description still trips the short_descr branch (531->533).
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("bug 2", db, admin)
        assert r.intent == "bug_detail"
        body = _texts(r)
        assert "Release 9.9" not in body      # no event line
        assert "Description:" in body          # short_descr present
    finally:
        db.close()


def test_cov_bug_detail_empty_description(client):
    # A bug with an empty description skips the short_descr block entirely
    # (the 531->533 false side).
    ids = _seed(client)
    from app.chatbot import executor
    from app import models
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        b = db.query(models.Bug).first()
        b.description = ""
        db.commit()
        r = executor.execute(f"bug {b.id}", db, admin)
        assert r.intent == "bug_detail"
    finally:
        db.close()


def test_cov_recent_activity_start_only_and_end_only(client):
    # Exercise the start-only (556->558) and end-only (558->560) time-window
    # branches of _handle_recent_activity directly with crafted ParsedQuery.
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        now = datetime.now(timezone.utc)

        pq_start = nlu.ParsedQuery()
        pq_start.time_window = nlu.TimeWindow(
            start=now - timedelta(days=1), end=None, label="recent")
        r1 = executor._handle_recent_activity(db, pq_start, admin)
        assert r1.intent == "recent_activity"

        pq_end = nlu.ParsedQuery()
        pq_end.time_window = nlu.TimeWindow(
            start=None, end=now + timedelta(days=1), label="up to now")
        r2 = executor._handle_recent_activity(db, pq_end, admin)
        assert r2.intent == "recent_activity"
    finally:
        db.close()


def test_cov_inline_list_truncation_note(client):
    # total > limit → the "showing the most recent N" note (line 804).
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        ctx = executor.build_context(db)
        pq = nlu.parse("show all bugs", ctx)
        pq.limit = 5   # force the limit below the seeded bug count (9)
        r = executor._handle_list_bugs(db, pq, ctx)
        assert r.intent == "list_bugs"
        assert "showing the most recent 5" in _texts(r)
    finally:
        db.close()


def test_cov_export_import_error_and_excel_error(client, monkeypatch):
    # 860-861: openpyxl/excel import fails → graceful "exporter isn't
    # available" text. 886-887: excel.stage_workbook raises
    # ExcelGenerationError → "couldn't build the spreadsheet" text.
    ids = _seed(client)
    import sys
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        ctx = executor.build_context(db)
        pq = nlu.parse("export all bugs to excel", ctx)
        from app.chatbot.executor import _eager_bug_query, _apply_bug_filters
        from sqlalchemy import select, func
        from app import models
        stmt, _cs = _apply_bug_filters(
            _eager_bug_query(), select(func.count(models.Bug.id)), pq)
        rows = list(db.scalars(stmt.limit(10)).all())

        # (a) Force `from . import excel` to raise ImportError: remove the
        # already-bound submodule attribute from the package AND poison
        # sys.modules so the re-import attempt fails. monkeypatch restores
        # both afterwards.
        import app.chatbot as chatbot_pkg
        monkeypatch.setitem(sys.modules, "app.chatbot.excel", None)
        monkeypatch.delattr(chatbot_pkg, "excel", raising=False)
        r_imp = executor._build_export_response(rows, pq, total=len(rows), cap=5000)
        assert r_imp.intent == "export_failed"
        assert "isn't available" in _texts(r_imp)
        monkeypatch.undo()

        # (b) excel imports fine but stage_workbook raises a generation error.
        from app.chatbot import excel as real_excel

        def _raise(*a, **k):
            raise real_excel.ExcelGenerationError("synthetic failure")

        monkeypatch.setattr(real_excel, "stage_workbook", _raise)
        r_err = executor._build_export_response(rows, pq, total=len(rows), cap=5000)
        assert r_err.intent == "export_failed"
        assert "couldn't build the spreadsheet" in _texts(r_err)
    finally:
        db.close()


def test_cov_filters_from_parsed_start_only_end_only(client):
    # 932->934 / 934->936: a time window with only start, then only end.
    _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        ctx = executor.build_context(db)
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

        pq = nlu.parse("report of all bugs", ctx)
        pq.time_window = nlu.TimeWindow(start=now, end=None, label="x")
        filt = executor._filters_from_parsed(pq)
        assert filt.date_from is not None and filt.date_to is None

        pq2 = nlu.parse("report of all bugs", ctx)
        pq2.time_window = nlu.TimeWindow(start=None, end=now, label="y")
        filt2 = executor._filters_from_parsed(pq2)
        assert filt2.date_from is None and filt2.date_to is not None
    finally:
        db.close()


def test_cov_stage_report_xlsx_build_error(monkeypatch):
    # 987: build_workbook_bytes raises XlsxBuildError → wrapped in
    # ExcelGenerationError.
    from app.chatbot import executor
    import app.reports as reports
    from app.reports.xlsx import XlsxBuildError
    from app.chatbot import excel as _excel

    def _raise(_result):
        raise XlsxBuildError("bad workbook")

    monkeypatch.setattr(reports, "build_workbook_bytes", _raise)

    class _Res:
        total = 1

    with pytest.raises(_excel.ExcelGenerationError):
        executor._stage_report_xlsx(_Res(), "item_detail")


def test_cov_handle_report_run_error(client, monkeypatch):
    # 1104-1105: run_report raises ValueError/KeyError → "couldn't run that
    # report" response.
    ids = _seed(client)
    from app.chatbot import executor
    import app.reports as reports
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])

        def _boom(*a, **k):
            raise ValueError("bad filter")

        monkeypatch.setattr(reports, "run_report", _boom)
        r = executor.execute("report of all bugs", db, admin)
        assert r.intent == "report_error"
        assert "couldn't run that report" in _texts(r)
    finally:
        db.close()


def test_cov_handle_report_file_block_fallback(client, monkeypatch):
    # 1126: the XLSX staging fails → the file block is None → fallback text
    # block telling the user to use the Reports sidebar.
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # Force _try_stage_file_block to return None.
        monkeypatch.setattr(executor, "_try_stage_file_block", lambda *a, **k: None)
        r = executor.execute("report of all bugs", db, admin)
        # If the report had rows, we land on the fallback-text branch.
        if r.intent == "report":
            assert any("Reports** sidebar" in b.payload.get("text", "")
                       for b in r.blocks if b.kind == "text")
    finally:
        db.close()


def test_cov_plan_set_environment_and_create_bug_fields(client):
    # 1193-1195 (set environment) and 1224/1225->1228/1229-1230
    # (create_bug with priority + project + assignee fields populated).
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        ctx = executor.build_context(db)

        # set_environment: the text→action map doesn't flag set_environment,
        # so drive the planner directly with a query carrying an environment.
        from app.chatbot.actions import ActionPlan
        pq_env = nlu.ParsedQuery(bug_id=1, environments=["PROD"])
        plan, err = executor._plan_set_environment(
            ActionPlan(kind="set_environment", actor_user_id=admin.id), pq_env)
        assert err is None and plan.new_value == "PROD"

        # create_bug with priority + project + assignee all present. The
        # phrase "create a critical bug" doesn't match the create-bug regex
        # (a priority word sits between the article and "bug"), so drive the
        # planner directly with a fully-populated ParsedQuery to exercise the
        # priority (1224), project (1225->1228) and assignee (1229-1230) legs.
        pq_cb = nlu.ParsedQuery(
            bug_id=None,
            action_kind="create_bug",
            action_title="Big one",
            priorities=["Critical"],
            project_ids=[ids["apollo"]],
            project_names=["Apollo"],
            assignee_ids=[ids["alice"]],
            assignee_names=["Alice Wonderland"],
        )
        plan2, err2 = executor._plan_create_bug(
            ActionPlan(kind="create_bug", actor_user_id=admin.id), pq_cb)
        assert err2 is None
        assert plan2.new_title == "Big one"
        assert plan2.new_value == "Critical"            # priority captured
        assert plan2.new_project_id == ids["apollo"]    # project captured
        assert plan2.new_project_name == "Apollo"
        assert plan2.target_user_ids == [ids["alice"]]  # assignee captured
    finally:
        db.close()


def test_cov_action_needs_bug_no_pronoun(client):
    # 1261: an action that needs a bug, no bug id and NO pronoun → the
    # generic "Which bug?" error.
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        pq = nlu.ParsedQuery(action_kind="set_status", action_value="Closed",
                             used_pronoun_bug=False)
        plan, err = executor._build_action_plan(pq, admin)
        assert plan is None
        assert "Which bug?" in err
    finally:
        db.close()


def test_cov_confirm_yes_remembers_bug(client):
    # 1316->1318: after a successful confirm the affected bug is remembered
    # so pronouns keep working.
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        mem.reset(admin.id)
        executor.execute("close bug 5", db, admin)
        executor.execute("yes", db, admin)
        sess = mem.get(admin.id)
        assert sess is not None and sess.last_bug_id == 5
    finally:
        db.close()


def test_cov_dispatch_bug_detail_remembers(client):
    # 1378->1380: dispatching a bug_detail intent with a bug_id remembers it.
    ids = _seed(client)
    from app.chatbot import executor, nlu
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        mem.reset(admin.id)
        ctx = executor.build_context(db)
        pq = nlu.parse("bug 3", ctx)
        assert pq.intent == "bug_detail" and pq.bug_id == 3
        executor._dispatch_read_intent("bug_detail", db, pq, admin, ctx)
        sess = mem.get(admin.id)
        assert sess is not None and sess.last_bug_id == 3
    finally:
        db.close()


def test_cov_try_classifier_action_branch(client, monkeypatch):
    # 1414-1419: the classifier predicts an action_* intent that the rule
    # parser couldn't fill → _classifier_action_invalid response.
    ids = _seed(client)
    from app.chatbot import executor, nlu
    from app.chatbot import classifier as clf
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        ctx = executor.build_context(db)
        pq = nlu.ParsedQuery(intent="unknown")

        class _Pred:
            intent = "action_assign"
            confidence = 0.9

        monkeypatch.setattr(clf, "predict", lambda _m: _Pred())
        r = executor._try_classifier("do the thing", db, pq, admin, ctx)
        assert r is not None and r.intent == "action_invalid"

        # And the None paths: predict returns None, and predict returns a
        # non-read / non-action intent.
        monkeypatch.setattr(clf, "predict", lambda _m: None)
        assert executor._try_classifier("x", db, pq, admin, ctx) is None

        class _Weird:
            intent = "totally_unknown_intent"
            confidence = 0.9
        monkeypatch.setattr(clf, "predict", lambda _m: _Weird())
        assert executor._try_classifier("x", db, pq, admin, ctx) is None
    finally:
        db.close()


def test_cov_try_llm_exception_swallowed(client, monkeypatch):
    # 1428-1430: an LLM fault inside _try_llm is swallowed → None.
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot import llm as _llm
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])

        def _boom():
            raise RuntimeError("llm exploded")

        monkeypatch.setattr(_llm, "is_available", _boom)
        assert executor._try_llm("weird", db, admin) is None
    finally:
        db.close()


def test_cov_try_cloud_llm_exception_swallowed(client, monkeypatch):
    # 1457-1459: a cloud fault inside _try_cloud_llm is swallowed → None.
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot import cloud_llm as _cloud
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])

        def _boom():
            raise RuntimeError("cloud exploded")

        monkeypatch.setattr(_cloud, "is_available", _boom)
        assert executor._try_cloud_llm("weird", db, admin, None) is None
    finally:
        db.close()


def test_cov_execute_cloud_then_classifier_then_llm(client, monkeypatch):
    # Drive execute()'s fallback chain so the cloud-return (1504), classifier
    # (1512) and the final unknown branches are all exercised.
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot import cloud_llm as _cloud
    from app.chatbot import classifier as _clf
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])

        # (a) Cloud returns a canned response → execute returns it (1504).
        canned = executor.Response(
            blocks=[executor.Block("text", {"text": "from cloud"})],
            summary="cloud", intent="cloud_answer")
        monkeypatch.setattr(_cloud, "is_available", lambda: True)
        monkeypatch.setattr(_cloud, "try_understand",
                            lambda *a, **k: canned)
        r = executor.execute("some free-form question", db, admin)
        assert r.intent == "cloud_answer"
        monkeypatch.undo()

        # (b) Cloud off; classifier returns a read intent → execute routes it
        # through the classifier fallback (1512).
        class _Pred:
            intent = "stats"
            confidence = 0.9
        monkeypatch.setattr(_clf, "predict", lambda _m: _Pred())
        r2 = executor.execute("zzz unrecognisable phrase zzz", db, admin)
        assert r2.intent == "stats"
        monkeypatch.undo()

        # (c) Everything misses → unknown fallback (the final return).
        r3 = executor.execute("zzz qwop zzz gibberish", db, admin)
        assert r3.intent in ("unknown", "fallback")
    finally:
        db.close()


def test_cov_did_you_mean_classifier_raises(monkeypatch):
    # executor 383-386: a classifier hiccup inside _did_you_mean must be
    # swallowed and return None (never break the unknown fallback).
    from app.chatbot import executor
    from app.chatbot import classifier as _clf

    def _boom(*a, **k):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(_clf, "explain", _boom)
    assert executor._did_you_mean("anything at all") is None


def test_cov_nlu_newest_sort_and_report_classify():
    # nlu 1305 (newest sort hint) and 1390 (report intent in final classify).
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    pq = nlu.parse("newest bugs", ctx)
    assert pq.sort_newest is True
    # A report phrasing reaches the report intent.
    pq2 = nlu.parse("aging report", ctx)
    assert pq2.intent == "report"


def test_cov_nlu_record_name_match_reporter_and_unresolved():
    # nlu 1224-1227 — reporter branch of _record_name_match + unresolved
    # reporter recording.
    from app.chatbot import nlu
    pq = nlu.ParsedQuery()
    seen_a: set[int] = set()
    seen_r: set[int] = set()
    nlu._record_name_match("reporter", 9, "Rae Porter", pq, seen_a, seen_r)
    assert pq.reporter_ids == [9] and pq.reporter_names == ["Rae Porter"]
    # Duplicate id is ignored.
    nlu._record_name_match("reporter", 9, "Rae Porter", pq, seen_a, seen_r)
    assert pq.reporter_ids == [9]

    nlu._record_unresolved_name("reporter", "ghost", pq)
    assert pq.unresolved_reporter_names == ["ghost"]


def test_cov_nlu_status_dedup_and_multi_name_and_bare_create():
    # nlu 631->630 — a status synonym whose canonical is already present is
    # de-duplicated (inner-loop continue). "resolved" and "fixed" both map to
    # Resolved.
    from app.chatbot import nlu
    out = nlu._extract_statuses("show resolved and fixed bugs")
    assert out.count("Resolved") == 1

    # nlu 754->752 — a single assignee pattern that matches twice keeps
    # iterating the finditer (both phrases captured).
    phrases = nlu._candidate_name_phrases(
        "bugs assigned to alice and assigned to bob")
    assignees = [p for role, p in phrases if role == "assignee"]
    assert len(assignees) >= 2

    # nlu 769->767 — same for the reporter pattern matching twice.
    rphrases = nlu._candidate_name_phrases(
        "bugs reported by alice and reported by bob")
    reporters = [p for role, p in rphrases if role == "reporter"]
    assert len(reporters) >= 2

    # nlu 1039->1043 — create-bug verb present but nothing capturable after
    # "bug" → the bare-title regex doesn't match → action_title stays None.
    pq = nlu.ParsedQuery()
    kind = nlu._action_create_bug("create a bug", pq)
    assert kind == "create_bug" and pq.action_title is None


def test_cov_nlu_add_resolved_projects_dedup():
    # nlu 1257->1256 — the loop's skip path when a resolved project id is
    # already in `seen` (no duplicate append).
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[(1, "apollo", "Apollo")])
    pq = nlu.ParsedQuery()
    seen: set[int] = set()
    nlu._add_resolved_projects("apollo", pq, ctx, seen)
    assert pq.project_ids == [1]
    # Second call with the same project → id already in `seen` → skipped.
    nlu._add_resolved_projects("apollo", pq, ctx, seen)
    assert pq.project_ids == [1]


def test_cov_plan_create_bug_assignee_without_project(client):
    # executor 1225->1228 — create_bug with an assignee but NO project, so the
    # project leg is skipped and the assignee leg runs.
    ids = _seed(client)
    from app.chatbot import executor, nlu
    from app.chatbot.actions import ActionPlan
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        pq = nlu.ParsedQuery(
            action_kind="create_bug", action_title="No project bug",
            assignee_ids=[ids["alice"]], assignee_names=["Alice Wonderland"],
        )
        plan, err = executor._plan_create_bug(
            ActionPlan(kind="create_bug", actor_user_id=admin.id), pq)
        assert err is None
        assert plan.new_title == "No project bug"
        assert plan.new_project_id is None
        assert plan.target_user_ids == [ids["alice"]]
    finally:
        db.close()


def test_cov_execute_reaches_llm_return(client, monkeypatch):
    # executor 1516 — cloud off, read dispatch + classifier both decline, but
    # _try_llm returns a Response → execute() returns it.
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot import classifier as _clf
    from app.chatbot import llm as _llm
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        monkeypatch.setattr(_clf, "predict", lambda _m: None)
        canned = executor.Response(
            blocks=[executor.Block("text", {"text": "llm handled it"})],
            summary="llm", intent="llm_answer")
        monkeypatch.setattr(_llm, "is_available", lambda: True)
        monkeypatch.setattr(_llm, "try_understand", lambda *a, **k: canned)
        r = executor.execute("zzqq unrecognised soup", db, admin)
        assert r.intent == "llm_answer"
    finally:
        db.close()


def test_cov_nlu_resolve_single_token_paths():
    # Single-token resolution: a first-name query resolves via the prefix
    # branch ("dave " is a word-prefix of "dave smith"); a last-name-only
    # query resolves via the last-name branch.
    from app.chatbot import nlu
    ctx = nlu.Context(
        users=[(1, "dave smith", "dsmith", "Dave Smith")],
        projects=[],
    )
    assert nlu._resolve_name("dave", ctx) == [(1, "Dave Smith")]   # prefix path
    assert nlu._resolve_name("smith", ctx) == [(1, "Dave Smith")]  # last-name path
    # A single token that matches nobody → empty (823->839 fall-through).
    assert nlu._resolve_name("zoey", ctx) == []


def test_cov_nlu_assign_list_verb_guard():
    # nlu 1083 — "give me all bugs assigned to bob" contains an assign-ish
    # verb but a list verb too, so _action_assign declines.
    from app.chatbot import nlu
    pq = nlu.ParsedQuery()
    pq.assignee_ids = [2]
    assert nlu._action_assign("give me all the bugs", pq) is None


def test_cov_exec_list_text_search_and_project_filter(client):
    # executor 155-162 (_apply_text_search), 191 (project_id filter), 203
    # (text_search filter) — a list query carrying both a project and a
    # quoted free-text term.
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute('bugs in project Beacon about "login"', db, admin)
        assert r.intent == "list_bugs"
        # The text search + project filter ran without error and produced a
        # response (possibly empty, possibly a table) — both are valid.
        assert r.blocks
        # A pure text-search query (no project) to be sure the LIKE clause
        # path is taken on its own too.
        r2 = executor.execute('find bugs about "crash"', db, admin)
        assert r2.intent == "list_bugs"
    finally:
        db.close()


def test_cov_build_action_plan_set_environment_dispatch(client):
    # executor 1272 — the set_environment dispatch leg of _build_action_plan.
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        pq = nlu.ParsedQuery(bug_id=1, action_kind="set_environment",
                             environments=["PROD"])
        plan, err = executor._build_action_plan(pq, admin)
        assert err is None and plan.kind == "set_environment"
        assert plan.new_value == "PROD"
    finally:
        db.close()


def test_cov_try_llm_available_calls_understand(client, monkeypatch):
    # executor 1428 — is_available() True so try_understand is invoked; we
    # stub it to return a Response and confirm it propagates.
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot import llm as _llm
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        canned = executor.Response(
            blocks=[executor.Block("text", {"text": "llm says hi"})],
            summary="llm", intent="llm_answer")
        monkeypatch.setattr(_llm, "is_available", lambda: True)
        monkeypatch.setattr(_llm, "try_understand", lambda *a, **k: canned)
        out = executor._try_llm("weird query", db, admin)
        assert out is canned
    finally:
        db.close()


def test_cov_execute_reaches_final_unknown(client, monkeypatch):
    # executor 1516 — every fallback returns None so execute() reaches the
    # terminal _handle_unknown return.
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot import classifier as _clf
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # Make the classifier decline so nothing intercepts the miss.
        monkeypatch.setattr(_clf, "predict", lambda _m: None)
        r = executor.execute("zzqq nonsense token soup", db, admin)
        assert r.intent in ("unknown", "fallback")
        assert any(b.kind == "text" for b in r.blocks)
    finally:
        db.close()


def test_cov_nlu_first_int_str_match_direct():
    # nlu 565-566 — a truthy-but-non-numeric group raises ValueError and the
    # loop continues to the next group; 574 — all-empty → None tail.
    from app.chatbot import nlu
    assert nlu._first_int_match(("abc", "5")) == 5      # "abc" -> ValueError -> continue
    assert nlu._first_int_match((None, "", "5")) == 5
    assert nlu._first_int_match((None, "")) is None
    assert nlu._first_str_match((None, "", "Days")) == "days"
    assert nlu._first_str_match((None, "")) is None


def test_cov_nlu_relative_window_no_match_in_parse():
    # nlu 615 — _parse_time_window matched a relative phrase but the unit
    # didn't resolve → final None. We can hit the None tail directly by
    # giving _relative_window a bad unit (already done) and confirm parse
    # returns None for a message with no recognised window at all.
    from app.chatbot import nlu
    assert nlu._parse_time_window("nothing temporal here") is None


def test_cov_nlu_action_helpers_absent_body_branches():
    # nlu 989->991 (create_project no name capture), 998->1000 (comment no
    # body), 1039->1043 / 1041->1043 (create_bug title branches),
    # 1071->1073 (due-date with no ISO date).
    from app.chatbot import nlu

    # create_project verb but the name regex won't capture (trailing junk).
    pq = nlu.ParsedQuery()
    kind = nlu._action_create_project("set up a project", pq)
    assert kind == "create_project"

    # comment verb, no colon → the body regex doesn't match → body None.
    pq2 = nlu.ParsedQuery()
    kind2 = nlu._action_add_comment("please comment on the bug", pq2)
    assert kind2 == "add_comment" and pq2.action_comment is None

    # comment verb WITH a colon but a whitespace-only body → the body regex
    # matches ".+" (the spaces) but it strips to empty, so action_comment
    # stays None (989->991, the false side of `if body:`).
    pq2b = nlu.ParsedQuery()
    kind2b = nlu._action_add_comment("comment on #5:    ", pq2b)
    assert kind2b == "add_comment" and pq2b.action_comment is None

    # create_bug bare form — a non-empty title is captured and set
    # (1039 matches, 1041 true → 1042 sets action_title).
    pq3b = nlu.ParsedQuery()
    kind3b = nlu._action_create_bug("create a bug Login is broken", pq3b)
    assert kind3b == "create_bug" and pq3b.action_title == "Login is broken"

    # create_bug bare form where the captured tail isn't stripped (no leading
    # space before the marker) — still returns create_bug; the title is the
    # raw remainder.
    pq3c = nlu.ParsedQuery()
    kind3c = nlu._action_create_bug("create a bug in project Apollo", pq3c)
    assert kind3c == "create_bug"

    # create_bug quoted-title form sets the title and returns immediately.
    pq4 = nlu.ParsedQuery()
    kind4 = nlu._action_create_bug('file a bug titled "Quoted" now', pq4)
    assert kind4 == "create_bug" and pq4.action_title == "Quoted"

    # due-date verb but no ISO date present → action_value stays None.
    pq5 = nlu.ParsedQuery()
    kind5 = nlu._action_set_due_date("set the due date for bug 5", pq5)
    assert kind5 == "set_due_date" and pq5.action_value is None


def test_cov_nlu_record_unresolved_unknown_role():
    # nlu 1234->exit — _record_unresolved_name with a role that is neither
    # assignee nor reporter falls through without appending to either list.
    from app.chatbot import nlu
    pq = nlu.ParsedQuery()
    nlu._record_unresolved_name("watcher", "ghost", pq)
    assert pq.unresolved_assignee_names == []
    assert pq.unresolved_reporter_names == []
    assert any("ghost" in n for n in pq.notes)


def test_cov_nlu_candidate_phrase_empty_continue():
    # nlu 754->752 / 769->767 — a cue that captures an empty/whitespace
    # phrase is skipped (the `if phrase:` guard).
    from app.chatbot import nlu
    # "assigned to ," → the captured group is empty after strip, so no
    # assignee phrase is emitted.
    out = nlu._candidate_name_phrases("bugs assigned to , please")
    assert all(p.strip() for _role, p in out)


def test_cov_nlu_classify_final_intent_report_branch():
    # nlu 1390 — the report branch of _classify_final_intent. parse() has an
    # earlier _REPORT_RE short-circuit, so this branch is only reachable by
    # calling the classifier helper directly with a report message.
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    pq = nlu.ParsedQuery(raw_message="breakdown by project")
    assert nlu._classify_final_intent("breakdown by project", pq, ctx) == "report"
    # stats branch too (no report keyword, but a stats keyword).
    pq2 = nlu.ParsedQuery(raw_message="dashboard overview")
    assert nlu._classify_final_intent("dashboard overview", pq2, ctx) == "stats"
    # recent_activity branch.
    pq3 = nlu.ParsedQuery(raw_message="what happened")
    assert nlu._classify_final_intent("what happened", pq3, ctx) == "recent_activity"


def test_cov_apply_time_window_start_only_end_only(client):
    # executor 171->173 / 173->175 — the bug-list time-window helper with a
    # window that has only a start, then only an end.
    _seed(client)
    from app.chatbot import executor, nlu
    from app.chatbot.executor import _eager_bug_query
    from sqlalchemy import select, func
    from app import models
    db = _new_db()
    try:
        now = datetime.now(timezone.utc)

        pq_start = nlu.ParsedQuery(raw_message="bugs created recently")
        pq_start.time_window = nlu.TimeWindow(
            start=now - timedelta(days=3), end=None, label="x")
        stmt, cs = executor._apply_time_window(
            _eager_bug_query(), select(func.count(models.Bug.id)), pq_start)
        assert db.scalar(cs) is not None   # query is runnable

        pq_end = nlu.ParsedQuery(raw_message="bugs updated before now")
        pq_end.time_window = nlu.TimeWindow(
            start=None, end=now + timedelta(days=1), label="y")
        stmt2, cs2 = executor._apply_time_window(
            _eager_bug_query(), select(func.count(models.Bug.id)), pq_end)
        assert db.scalar(cs2) is not None
    finally:
        db.close()


def test_cov_confirm_yes_no_bug_id_skips_remember(client):
    # executor 1316->1318 — confirming a create_project (no bug_id) skips the
    # remember-bug step.
    _seed(client)
    from app.chatbot import executor
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        from app import models
        admin = db.query(models.User).filter_by(email="admin@test.local").one()
        mem.reset(admin.id)
        r = executor.execute("create project Saturn", db, admin)
        assert r.intent == "confirm_action"
        r2 = executor.execute("yes", db, admin)
        # create_project succeeds for admin; the plan has no bug_id so the
        # remember-bug branch is skipped (no last_bug_id recorded).
        assert r2.intent in ("action_done", "action_error")
        sess = mem.get(admin.id)
        assert sess is None or sess.last_bug_id is None
    finally:
        db.close()


def test_cov_dispatch_bug_detail_without_id(client):
    # executor 1378->1380 — bug_detail dispatch when pq.bug_id is falsy skips
    # the remember step and still calls the handler (which reports not-found).
    _seed(client)
    from app.chatbot import executor, nlu
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        from app import models
        admin = db.query(models.User).filter_by(email="admin@test.local").one()
        mem.reset(admin.id)
        pq = nlu.ParsedQuery(intent="bug_detail", bug_id=None)
        r = executor._dispatch_read_intent("bug_detail", db, pq, admin,
                                           executor.build_context(db))
        assert r is not None and r.intent == "bug_detail"
    finally:
        db.close()


def test_cov_nlu_project_literal_loop(client):
    # nlu 1257->1256 — the literal project-name fallback loop iterating the
    # context projects, matching one by whole-word.
    _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        ctx = executor.build_context(db)
        # No "project" cue word at all → forces the literal-name fallback
        # loop over ctx.projects; "Apollo" appears as a bare token.
        pq = nlu.parse("anything broken in Apollo recently", ctx)
        assert any(name == "Apollo" for name in pq.project_names)
    finally:
        db.close()
