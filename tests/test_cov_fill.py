"""Supplemental coverage for narrow defensive branches that the per-module
coverage agents correctly identified as unreachable over HTTP, but which ARE
reachable (and worth pinning) as direct unit tests of the route helpers.

Each test exercises a guard that protects a real invariant — "this is your own
session", "don't strand the system with no admin" — that the HTTP layer happens
to pre-empt with an earlier guard, leaving the inner guard uncovered by
end-to-end tests. Calling the helper directly keeps the safety net tested.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select


def test_cov_is_current_false_on_unparseable_cookie(client):
    """app/routes/sessions.py:44 — `_is_current` returns False when the request
    has no / a garbage session cookie. Over HTTP the admin's own cookie always
    parses (auth already validated it), so reach the branch directly."""
    import app.routes.sessions as sessions

    class _Req:
        def __init__(self, cookies):
            self.cookies = cookies

    sess = type("S", (), {"jti": "whatever"})()
    # No cookie at all → parse fails → False.
    assert sessions._is_current(_Req({}), sess) is False
    # Present but unparseable → parse fails → False.
    assert sessions._is_current(_Req({sessions.COOKIE_NAME: "not-a-real-token"}), sess) is False


def test_cov_last_admin_guardrail_blocks_demoting_sole_admin(client):
    """app/routes/users.py:177-181 — the last-admin guardrail raises 400 when
    demoting the only admin. The route pre-empts this via the self-edit guard
    (you can't demote yourself), so exercise the helper directly against a
    fresh DB whose sole admin is the bootstrap admin."""
    import app.routes.users as users
    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.role == "admin"))
        assert admin is not None, "bootstrap admin should exist after startup"
        with pytest.raises(HTTPException) as ei:
            users._check_last_admin_guardrail(db, admin, admin.id, {"role": "user"})
        assert ei.value.status_code == 400
        assert "last admin" in ei.value.detail.lower()
        # And deactivating the sole admin is likewise blocked.
        with pytest.raises(HTTPException):
            users._check_last_admin_guardrail(db, admin, admin.id, {"is_active": False})
    finally:
        db.close()


def test_cov_delete_last_admin_blocked(client):
    """app/routes/users.py:261-270 — deleting the only admin is rejected. Over
    HTTP `require_admin` guarantees the actor is itself an active admin (so a
    second admin always exists), pre-empting this guard; call the route
    function directly with a non-admin actor to pin the defensive branch."""
    import app.routes.users as users
    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        # A distinct, non-admin actor so actor.id != target and the
        # "delete yourself" guard doesn't fire first.
        actor = User(name="probe", email="probe.cov@test.local", role="user",
                     is_active=True, password_hash="x")
        db.add(actor)
        db.flush()
        with pytest.raises(HTTPException) as ei:
            users.delete_user(admin.id, db=db, actor=actor)
        assert ei.value.status_code == 400
        assert "last admin" in ei.value.detail.lower()
        db.rollback()
    finally:
        db.close()
