"""Tests for the chatbot actions, router, excel, redaction, and classifier
error and edge branches.

This file covers the failure paths: permission denials, not-found targets,
invalid field values, the rollback exception handler, router rate-limit
eviction, transcript persistence, graceful executor-crash handling, the Excel
cache eviction and expiry paths, the redaction fail-closed branch, and the
classifier's empty-example skip.

  - The `client` fixture re-imports every ``app.*`` module per test, so all
    app imports happen inside the test bodies to bind the current generation.
  - To toggle a config flag we set the ``config.Settings`` class attribute via
    ``monkeypatch`` (module-level refs go stale on reimport).
  - DB objects are created through ``app.database.SessionLocal`` which, after
    the fixture runs, points at the per-test temp SQLite file. The bootstrap
    admin already exists; we add extra users/projects/bugs directly.
  - Hermetic: SLEUTH_CLOUD_ENABLED is 0 (conftest) so no network is touched.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Small seeding helpers — used by the actions tests. They run against the
# live (re-imported) app.database / app.models for the current test.
# ---------------------------------------------------------------------------
def _seed_basic():
    """Create alice (manager), bob (user), one project + one bug reported by
    alice and assigned to bob. Returns a dict of ids.

    Uses a fresh SessionLocal session and closes it; callers open their own
    session afterwards so they see committed rows.
    """
    from app.database import SessionLocal
    from app import models
    from app.auth import hash_password

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        alice = models.User(name="Alice Wonderland", email="alice@cov.local",
                            role="manager",
                            password_hash=hash_password("Alice12345"),
                            is_active=True)
        bob = models.User(name="Bob Builder", email="bob@cov.local",
                          role="user",
                          password_hash=hash_password("Bobby12345"),
                          is_active=True)
        db.add_all([alice, bob])
        db.commit()
        proj = models.Project(name="Apollo Cov", description="d")
        db.add(proj)
        db.commit()
        bug = models.Bug(title="Login broken on Safari", description="d",
                         status="New", priority="High", environment="PROD",
                         project_id=proj.id, reporter_id=alice.id)
        db.add(bug)
        db.commit()
        bug.assignees = [bob]
        db.commit()
        return {
            "admin_id": admin.id,
            "alice_id": alice.id,
            "bob_id": bob.id,
            "proj_id": proj.id,
            "bug_id": bug.id,
        }
    finally:
        db.close()


# ===========================================================================
# actions.py
# ===========================================================================
def test_cov_create_bug_inactive_account(client):
    """actions.py:144 + 332 — _check_can_create_bug rejects an inactive actor,
    and _apply_create_bug surfaces that error."""
    ids = _seed_basic()
    from app.database import SessionLocal
    from app import models
    from app.chatbot import actions

    db = SessionLocal()
    try:
        bob = db.get(models.User, ids["bob_id"])
        bob.is_active = False
        db.commit()
        # 144: helper returns the inactive string.
        assert actions._check_can_create_bug(bob) == "Your account is inactive"
        # 332: _apply_create_bug returns the error response from that helper.
        # Call the applier directly: execute_plan now gates all non-admins via
        # sleuth_write_denied (admin-only Sleuth writes), so the per-action
        # inactive-account branch is reached by calling _apply_create_bug.
        plan = actions.ActionPlan(
            kind="create_bug", actor_user_id=bob.id,
            new_title="Anything", summary_human="Create bug",
        )
        resp = actions._apply_create_bug(db, plan, bob)
        assert resp.intent == "action_error"
        assert "inactive" in resp.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_success_response_without_bug_id(client):
    """actions.py:190->201 — _success_response with bug_id=None skips the
    suggestions block and returns a single text block."""
    from app.chatbot import actions

    resp = actions._success_response("plain done message", bug_id=None)
    assert resp.intent == "action_done"
    assert len(resp.blocks) == 1
    assert resp.blocks[0].kind == "text"
    assert resp.blocks[0].payload["text"] == "plain done message"


def test_cov_assign_bug_not_found_and_perm_denied(client):
    """actions.py:228 (bug missing) + 231 (permission denied) for assign."""
    ids = _seed_basic()
    from app.database import SessionLocal
    from app import models
    from app.chatbot import actions

    db = SessionLocal()
    try:
        alice = db.get(models.User, ids["alice_id"])
        bob = db.get(models.User, ids["bob_id"])

        # 228 — bug_id points at a non-existent bug.
        plan_missing = actions.ActionPlan(
            kind="assign", actor_user_id=alice.id, bug_id=987654,
            target_user_ids=[bob.id], target_user_names=["Bob"],
            summary_human="Assign",
        )
        # Call the applier directly: execute_plan now gates non-admins via the
        # admin-only Sleuth-write policy, so the per-action branches (missing
        # bug, per-type permission) are exercised through _apply_assign.
        r_missing = actions._apply_assign(db, plan_missing, alice)
        assert r_missing.intent == "action_error"
        assert "not found" in r_missing.blocks[0].payload["text"].lower()

        # 231 — make the work item a Task so a regular user (bob) is denied
        # edit permission (chat/REST parity).
        bug = db.get(models.Bug, ids["bug_id"])
        if hasattr(bug, "item_type"):
            bug.item_type = "Task"
            db.commit()
            plan_perm = actions.ActionPlan(
                kind="assign", actor_user_id=bob.id, bug_id=ids["bug_id"],
                target_user_ids=[bob.id], target_user_names=["Bob"],
                summary_human="Assign",
            )
            r_perm = actions._apply_assign(db, plan_perm, bob)
            assert r_perm.intent == "action_error"
            assert "permission" in r_perm.blocks[0].payload["text"].lower()
        else:  # pragma: no cover - models always have item_type in this build
            pytest.skip("Bug has no item_type column; cannot exercise 231")
    finally:
        db.close()


def test_cov_unassign_branches(client):
    """actions.py:256 (bug missing) + 259 (perm denied) + 265 (no-op)."""
    ids = _seed_basic()
    from app.database import SessionLocal
    from app import models
    from app.chatbot import actions

    db = SessionLocal()
    try:
        alice = db.get(models.User, ids["alice_id"])
        bob = db.get(models.User, ids["bob_id"])

        # Per-action branches are exercised via the applier directly, since
        # execute_plan now gates non-admins (admin-only Sleuth writes).
        # 256 — bug missing.
        r_missing = actions._apply_unassign(
            db,
            actions.ActionPlan(kind="unassign", actor_user_id=alice.id,
                               bug_id=987654, target_user_ids=[bob.id],
                               target_user_names=["Bob"],
                               summary_human="Unassign"),
            alice,
        )
        assert r_missing.intent == "action_error"
        assert "not found" in r_missing.blocks[0].payload["text"].lower()

        bug = db.get(models.Bug, ids["bug_id"])
        has_item_type = hasattr(bug, "item_type")

        # 259 — permission denied: bob (user) on a Task.
        if has_item_type:
            bug.item_type = "Task"
            db.commit()
            r_perm = actions._apply_unassign(
                db,
                actions.ActionPlan(kind="unassign", actor_user_id=bob.id,
                                   bug_id=ids["bug_id"],
                                   target_user_ids=[bob.id],
                                   target_user_names=["Bob"],
                                   summary_human="Unassign"),
                bob,
            )
            assert r_perm.intent == "action_error"
            assert "permission" in r_perm.blocks[0].payload["text"].lower()
            # reset back to Bug so the next assertion's edit is allowed
            bug.item_type = "Bug"
            db.commit()

        # 265 — no-op: drop a user that isn't assigned. Now reported as a
        # skip (intent action_noop), not an error.
        r_noop = actions._apply_unassign(
            db,
            actions.ActionPlan(kind="unassign", actor_user_id=alice.id,
                               bug_id=ids["bug_id"],
                               target_user_ids=[999999],
                               target_user_names=["Ghost"],
                               summary_human="Unassign ghost"),
            alice,
        )
        assert r_noop.intent == "action_noop"
        assert "nothing changed" in r_noop.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_set_field_permission_denied(client):
    """actions.py:285 — _apply_set_field permission-denied branch."""
    ids = _seed_basic()
    from app.database import SessionLocal
    from app import models
    from app.chatbot import actions

    db = SessionLocal()
    try:
        bug = db.get(models.Bug, ids["bug_id"])
        if not hasattr(bug, "item_type"):  # pragma: no cover
            pytest.skip("No item_type column; cannot exercise 285")
        bug.item_type = "Requirement"
        db.commit()
        bob = db.get(models.User, ids["bob_id"])
        # set_status routes through _apply_set_field — bob can't edit a
        # Requirement. Call the applier directly (execute_plan now gates
        # non-admins before the per-action check).
        resp = actions._apply_set_field(
            db,
            actions.ActionPlan(kind="set_status", actor_user_id=bob.id,
                               bug_id=ids["bug_id"], new_value="Closed",
                               summary_human="Set status"),
            bob, "status", "status",
        )
        assert resp.intent == "action_error"
        assert "permission" in resp.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_add_comment_empty_and_too_long(client):
    """actions.py:309 (no body) + 314 (body > 4000 chars)."""
    ids = _seed_basic()
    from app.database import SessionLocal
    from app import models
    from app.chatbot import actions

    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin_id"])

        # 309 — whitespace-only body counts as empty.
        r_empty = actions.execute_plan(
            actions.ActionPlan(kind="add_comment", actor_user_id=admin.id,
                               bug_id=ids["bug_id"], comment_body="   ",
                               summary_human="Comment"),
            db, admin,
        )
        assert r_empty.intent == "action_error"
        assert "comment text" in r_empty.blocks[0].payload["text"].lower()

        # 314 — body over the 4000-char cap.
        r_long = actions.execute_plan(
            actions.ActionPlan(kind="add_comment", actor_user_id=admin.id,
                               bug_id=ids["bug_id"], comment_body="x" * 4001,
                               summary_human="Comment"),
            db, admin,
        )
        assert r_long.intent == "action_error"
        assert "too long" in r_long.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_create_bug_title_validation_and_targets(client):
    """actions.py:335 (no title) + 340 (title > 200) + 352 (bad project) +
    365-368 (assign targets on create)."""
    ids = _seed_basic()
    from app.database import SessionLocal
    from app import models
    from app.chatbot import actions

    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin_id"])

        # 335 — empty title.
        r_empty = actions.execute_plan(
            actions.ActionPlan(kind="create_bug", actor_user_id=admin.id,
                               new_title="   ", summary_human="Create"),
            db, admin,
        )
        assert r_empty.intent == "action_error"
        assert "title" in r_empty.blocks[0].payload["text"].lower()

        # 340 — title too long.
        r_long = actions.execute_plan(
            actions.ActionPlan(kind="create_bug", actor_user_id=admin.id,
                               new_title="t" * 201, new_project_id=ids["proj_id"],
                               summary_human="Create"),
            db, admin,
        )
        assert r_long.intent == "action_error"
        assert "too long" in r_long.blocks[0].payload["text"].lower()

        # 352 — explicit project id that doesn't exist.
        r_badproj = actions.execute_plan(
            actions.ActionPlan(kind="create_bug", actor_user_id=admin.id,
                               new_title="Valid title", new_project_id=888777,
                               summary_human="Create"),
            db, admin,
        )
        assert r_badproj.intent == "action_error"
        assert "doesn't exist" in r_badproj.blocks[0].payload["text"].lower()

        # 365-368 — create with assignee targets attaches them.
        before = db.query(models.Bug).count()
        r_ok = actions.execute_plan(
            actions.ActionPlan(kind="create_bug", actor_user_id=admin.id,
                               new_title="Created with assignee",
                               new_project_id=ids["proj_id"],
                               target_user_ids=[ids["bob_id"]],
                               target_user_names=["Bob Builder"],
                               summary_human="Create"),
            db, admin,
        )
        assert r_ok.intent == "action_done"
        assert db.query(models.Bug).count() == before + 1
        new_bug = (db.query(models.Bug)
                   .order_by(models.Bug.id.desc()).first())
        assert ids["bob_id"] in [a.id for a in new_bug.assignees]
    finally:
        db.close()


def test_cov_create_bug_no_projects_exist(client):
    """actions.py:347 — when no project is given and none exist, error out.

    The conftest bootstrap creates an admin but no projects, so a fresh DB
    (without _seed_basic) has zero Project rows. We delete any stragglers to
    be certain, then create a bug with no project id.
    """
    from app.database import SessionLocal
    from app import models
    from app.chatbot import actions

    db = SessionLocal()
    try:
        # Ensure no projects/bugs (bugs FK projects) exist.
        db.query(models.Bug).delete()
        db.query(models.Project).delete()
        db.commit()
        admin = db.query(models.User).filter_by(role="admin").first()
        resp = actions.execute_plan(
            actions.ActionPlan(kind="create_bug", actor_user_id=admin.id,
                               new_title="Orphan bug", new_project_id=None,
                               summary_human="Create"),
            db, admin,
        )
        assert resp.intent == "action_error"
        assert "no projects" in resp.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_create_project_validation(client):
    """actions.py:384 (no name) + 386 (name > 120) + 392 (duplicate name)."""
    ids = _seed_basic()
    from app.database import SessionLocal
    from app import models
    from app.chatbot import actions

    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin_id"])

        # 384 — empty name.
        r_empty = actions.execute_plan(
            actions.ActionPlan(kind="create_project", actor_user_id=admin.id,
                               new_project_name="   ", summary_human="Create"),
            db, admin,
        )
        assert r_empty.intent == "action_error"
        assert "name" in r_empty.blocks[0].payload["text"].lower()

        # 386 — name too long.
        r_long = actions.execute_plan(
            actions.ActionPlan(kind="create_project", actor_user_id=admin.id,
                               new_project_name="p" * 121,
                               summary_human="Create"),
            db, admin,
        )
        assert r_long.intent == "action_error"
        assert "too long" in r_long.blocks[0].payload["text"].lower()

        # 392 — duplicate (case-insensitive) of the seeded "Apollo Cov".
        r_dupe = actions.execute_plan(
            actions.ActionPlan(kind="create_project", actor_user_id=admin.id,
                               new_project_name="apollo cov",
                               summary_human="Create"),
            db, admin,
        )
        assert r_dupe.intent == "action_error"
        assert "already a project" in r_dupe.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_execute_plan_actor_mismatch_unknown_and_exception(client):
    """actions.py:417 (actor mismatch) + 429/431 (env + due-date dispatch) +
    438 (unknown kind) + 439-446 (rollback exception handler)."""
    ids = _seed_basic()
    from app.database import SessionLocal
    from app import models
    from app.chatbot import actions

    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin_id"])

        # 417 — plan staged for a different user id than the actor.
        r_mismatch = actions.execute_plan(
            actions.ActionPlan(kind="set_status", actor_user_id=999999,
                               bug_id=ids["bug_id"], new_value="Closed",
                               summary_human="x"),
            db, admin,
        )
        assert r_mismatch.intent == "action_error"
        assert "different user" in r_mismatch.blocks[0].payload["text"].lower()

        # 429 — set_environment dispatch (admin may edit; PROD->DEV).
        r_env = actions.execute_plan(
            actions.ActionPlan(kind="set_environment", actor_user_id=admin.id,
                               bug_id=ids["bug_id"], new_value="DEV",
                               summary_human="env"),
            db, admin,
        )
        assert r_env.intent == "action_done"
        db.expire_all()
        assert db.get(models.Bug, ids["bug_id"]).environment == "DEV"

        # 431 — set_due_date dispatch.
        r_due = actions.execute_plan(
            actions.ActionPlan(kind="set_due_date", actor_user_id=admin.id,
                               bug_id=ids["bug_id"], new_value="2026-06-15",
                               summary_human="due"),
            db, admin,
        )
        assert r_due.intent == "action_done"

        # 438 — a kind the dispatch chain doesn't recognise.
        r_unknown = actions.execute_plan(
            actions.ActionPlan(kind="totally_bogus", actor_user_id=admin.id,
                               summary_human="x"),
            db, admin,
        )
        assert r_unknown.intent == "action_error"
        assert "unknown action" in r_unknown.blocks[0].payload["text"].lower()

        # 439-446 — force an exception inside the try: a non-string
        # comment_body makes (body or "").strip() raise AttributeError, which
        # the handler catches, rolls back, and reports as "Action failed".
        r_exc = actions.execute_plan(
            actions.ActionPlan(kind="add_comment", actor_user_id=admin.id,
                               bug_id=ids["bug_id"],
                               comment_body=12345,  # type: ignore[arg-type]
                               summary_human="x"),
            db, admin,
        )
        assert r_exc.intent == "action_error"
        assert "action failed" in r_exc.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_execute_plan_rollback_itself_fails(client, monkeypatch):
    """actions.py:444-445 — when the outer handler's db.rollback() also raises
    SQLAlchemyError, the inner try/except swallows it (best-effort) and we
    still return an 'Action failed' error response.
    """
    ids = _seed_basic()
    from app.database import SessionLocal
    from app import models
    from app.chatbot import actions
    from sqlalchemy.exc import SQLAlchemyError

    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin_id"])

        # Make rollback explode so the inner `except SQLAlchemyError: pass`
        # (444-445) executes.
        def boom_rollback():
            raise SQLAlchemyError("rollback also broke")
        monkeypatch.setattr(db, "rollback", boom_rollback)

        # Outer exception trigger: non-string comment body -> AttributeError.
        resp = actions.execute_plan(
            actions.ActionPlan(kind="add_comment", actor_user_id=admin.id,
                               bug_id=ids["bug_id"],
                               comment_body=999,  # type: ignore[arg-type]
                               summary_human="x"),
            db, admin,
        )
        assert resp.intent == "action_error"
        assert "action failed" in resp.blocks[0].payload["text"].lower()
    finally:
        # Restore happens via monkeypatch teardown before close.
        db.close()


# ===========================================================================
# router.py
# ===========================================================================
def test_cov_persist_turn_disabled(client, monkeypatch):
    """router.py:60 — _persist_turn early-returns when chat memory is off.

    With the flag off, no chat_* rows are written even though the request
    succeeds.
    """
    import app.config as config
    monkeypatch.setattr(config.Settings, "SLEUTH_CHAT_MEMORY_ENABLED", False)

    res = client.post("/api/auth/login", json={
        "email": "admin@test.local", "password": "Admin1234",
    })
    assert res.status_code == 200

    from app.database import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        before = db.query(models.ChatMessage).count()
    finally:
        db.close()

    r = client.post("/api/chat/ask", json={"message": "summary"})
    assert r.status_code == 200

    db = SessionLocal()
    try:
        after = db.query(models.ChatMessage).count()
    finally:
        db.close()
    assert after == before  # 60: nothing persisted when disabled


def test_cov_persist_turn_cloud_engine_label(admin_client):
    """router.py:77 — _persist_turn tags the assistant row engine='cloud'
    when the response intent starts with 'cloud_'. We call _persist_turn
    directly with a synthetic cloud response so no network is needed."""
    from app.database import SessionLocal
    from app import models
    from app.chatbot import router, executor

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        resp = executor.Response(
            blocks=[executor.Block("text", {"text": "cloud said hi"})],
            summary="cloud reply", intent="cloud_answer",
        )
        router._persist_turn(db, admin, "hello there", resp)
        row = (db.query(models.ChatMessage)
               .filter_by(role="assistant")
               .order_by(models.ChatMessage.id.desc()).first())
        assert row is not None
        assert row.engine == "cloud"  # 77
    finally:
        db.close()


def test_cov_persist_turn_swallows_exception(admin_client, monkeypatch):
    """router.py:93-95 — a failure inside _persist_turn is caught, rolled
    back, and logged (best-effort); it must not propagate."""
    from app.database import SessionLocal
    from app import models
    from app.chatbot import router, executor

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()

        # Make the very first DB touch inside the try: explode.
        def boom(*a, **k):
            raise RuntimeError("simulated persist failure")
        monkeypatch.setattr(db, "query", boom)

        resp = executor.Response(
            blocks=[executor.Block("text", {"text": "hi"})],
            summary="s", intent="stats",
        )
        # Must not raise — the except on 93-95 swallows it.
        router._persist_turn(db, admin, "summary", resp)
    finally:
        # db.query is restored by monkeypatch teardown before close.
        db.close()


def test_cov_check_rate_evicts_stale_then_allows(client):
    """router.py:117 — _check_rate pops timestamps older than the window so a
    user who was busy a minute ago isn't blocked now."""
    from app.chatbot import router

    uid = 4242
    router._rate_state.pop(uid, None)
    # Seed the bucket with stale timestamps (well outside the window).
    stale = 1000.0
    router._rate_state[uid] = [stale, stale + 1, stale + 2]
    # A real 'now' is far ahead of `stale`, so the while-loop pops them all
    # (117) and the call succeeds without raising 429.
    router._check_rate(uid)
    # All stale entries gone; exactly one fresh timestamp remains.
    assert len(router._rate_state[uid]) == 1
    router._rate_state.pop(uid, None)


def test_cov_ask_passes_through_httpexception(admin_client, monkeypatch):
    """router.py:171 — an HTTPException raised by the executor propagates out
    of /ask (it is not swallowed into a graceful 200)."""
    from fastapi import HTTPException
    from app.chatbot import executor

    def raise_http(*a, **k):
        raise HTTPException(status_code=403, detail="nope from executor")
    monkeypatch.setattr(executor, "execute", raise_http)

    r = admin_client.post("/api/chat/ask", json={"message": "summary"})
    assert r.status_code == 403
    assert "nope from executor" in r.text


def test_cov_ask_graceful_on_executor_crash(admin_client, monkeypatch):
    """router.py:174-178 — a non-HTTP exception is caught and turned into a
    friendly 200 error reply (intent='error') instead of crashing."""
    from app.chatbot import executor

    def raise_value(*a, **k):
        raise ValueError("kaboom inside executor")
    monkeypatch.setattr(executor, "execute", raise_value)

    r = admin_client.post("/api/chat/ask", json={"message": "summary"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "error"
    assert "something went wrong" in body["blocks"][0]["payload"]["text"].lower()


# ===========================================================================
# excel.py
# ===========================================================================
def test_cov_excel_evict_expired(client):
    """excel.py:69 — _evict_expired_locked drops entries whose expiry has
    passed."""
    from app.chatbot import excel
    excel.clear_all_for_test()
    now = 10_000.0
    # One expired, one live (relative to `now`).
    excel._cache["dead"] = (b"x", "a.xlsx", now - 1, 1)
    excel._cache["live"] = (b"y", "b.xlsx", now + 1000, 1)
    excel._evict_expired_locked(now)  # 69: pops 'dead'
    assert "dead" not in excel._cache
    assert "live" in excel._cache
    excel.clear_all_for_test()


def test_cov_excel_evict_oldest_over_cap(client):
    """excel.py:77-78 — _evict_oldest_locked drops the soonest-expiring entry
    once the cache is at capacity."""
    from app.chatbot import excel
    excel.clear_all_for_test()
    cap = excel._MAX_ENTRIES
    base = 50_000.0
    # Fill exactly to capacity; 'tok0' has the soonest expiry.
    for i in range(cap):
        excel._cache[f"tok{i}"] = (b"x", f"f{i}.xlsx", base + i, 1)
    assert len(excel._cache) == cap
    excel._evict_oldest_locked()  # 77 (len check passes) + 78 (pop oldest)
    assert "tok0" not in excel._cache
    assert len(excel._cache) == cap - 1
    excel.clear_all_for_test()


def test_cov_excel_build_workbook_unavailable(client, monkeypatch):
    """excel.py:119 — _build_workbook raises ExcelGenerationError when
    openpyxl is reported unavailable."""
    from app.chatbot import excel
    monkeypatch.setattr(excel, "OPENPYXL_AVAILABLE", False)
    with pytest.raises(excel.ExcelGenerationError):
        excel._build_workbook([{"id": 1, "title": "t"}], "desc")


def test_cov_excel_fetch_staged_empty_token_and_expired(client, monkeypatch):
    """excel.py:200 (falsy token -> None) + 209-210 (expired entry popped).

    fetch_staged sweeps _evict_expired_locked() first (line 203), which uses
    the same `exp <= now` predicate as the explicit guard on line 208, so a
    truly-expired entry is already gone before the guard runs, making 209-210
    a defensive last line. We isolate that guard by no-opping the sweep, then
    insert an entry whose expiry is in the past so line 208's `expires <= now`
    fires the pop+return on 209-210.
    """
    from app.chatbot import excel
    excel.clear_all_for_test()

    # 200 — empty/None token short-circuits to None.
    assert excel.fetch_staged("", 1) is None

    # Freeze the clock and bypass the upfront eviction sweep.
    frozen = 9_999_999.0
    monkeypatch.setattr(excel.time, "time", lambda: frozen)
    monkeypatch.setattr(excel, "_evict_expired_locked", lambda now: None)
    # Entry already expired relative to `frozen` (expires < now). 4-tuple now
    # carries the owner id as the last element.
    excel._cache["stale_tok"] = (b"data", "f.xlsx", frozen - 1, 1)
    assert excel.fetch_staged("stale_tok", 1) is None   # expired -> popped
    assert "stale_tok" not in excel._cache              # popped on expiry
    excel.clear_all_for_test()


# ===========================================================================
# redaction.py
# ===========================================================================
def test_cov_redaction_fail_closed(client, monkeypatch):
    """redaction.py:75-77 — if a pattern substitution raises, redact() fails
    closed and returns [REDACTED] rather than leaking the raw text."""
    from app.chatbot import redaction

    # Replace the compiled-pattern list with one whose .sub blows up.
    class _Boom:
        def sub(self, *a, **k):
            raise RuntimeError("regex exploded")

    monkeypatch.setattr(redaction, "_PATTERNS", [(_Boom(), redaction._REDACTED)])
    out = redaction.redact("some text with a maybe-secret in it")
    assert out == redaction._REDACTED  # 75-77 fail-closed path


# ===========================================================================
# classifier.py
# ===========================================================================
def test_cov_classifier_train_skips_empty_example(client):
    """classifier.py:185 — _train skips corpus examples that tokenize to
    nothing (all-stopword / empty phrasings)."""
    from app.chatbot import classifier

    # "the a is to of" are all stopwords -> _tokenize returns [] -> line 185
    # `continue` skips it. "list bugs" still produces a usable doc, so the
    # trained model isn't empty.
    corpus = [
        ("list_bugs", ["the a is to of", "", "list bugs"]),
    ]
    model = classifier._train(corpus)
    # Only the one non-empty example survived as a doc.
    assert len(model.docs) == 1
    assert model.docs[0][0] == "list_bugs"
