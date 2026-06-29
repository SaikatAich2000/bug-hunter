"""Tests for the authentication routes (app/routes/auth.py) and the auth
helpers (app/auth.py).

Why everything is imported inside each test
-------------------------------------------
The ``client`` fixture (conftest.py) deletes and re-imports every ``app.*``
module between tests so each test picks up its own env overrides. A module
captured at the top level goes stale the moment any client-using test has run.
Importing inside the test body ensures we bind to the same import generation
the running app is using. To patch app.config, set the ``Settings`` class
attribute directly (module-level instances go stale).

The reset-flow tests insert PasswordResetToken rows directly via the app's
``SessionLocal`` using ``generate_reset_token()``, so we hold the raw token
without reading an email. This lets us build valid, expired, already-used, and
unknown-token cases deterministically. The email backend is disabled in tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD


# ---------------------------------------------------------------------------
# Small helpers — kept local so re-imports never leave a stale module reference.
# ---------------------------------------------------------------------------
def _admin_user_id(client) -> int:
    """Log in as admin and return the bootstrap admin's id via /api/auth/me."""
    res = client.post("/api/auth/login", json={
        "email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD,
    })
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _insert_reset_token(user_id: int, *, expires_in_hours: float = 2.0,
                        used: bool = False) -> str:
    """Create a PasswordResetToken row directly and return the raw token.

    Mirrors what forgot_password() stores (a sha256 hash of a random token),
    but keeps the plaintext so tests can drive reset-password without reading
    an email. expires_in_hours<0 gives an expired token; used=True stamps
    used_at to exercise the already-used branch.
    """
    # Import inside the call to bind to the current app generation (and its
    # SQLite engine), not a stale one.
    from app.auth import generate_reset_token
    from app.database import SessionLocal
    from app.models import PasswordResetToken

    raw, token_hash = generate_reset_token()
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        prt = PasswordResetToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=now + timedelta(hours=expires_in_hours),
            used_at=now if used else None,
        )
        db.add(prt)
        db.commit()
    finally:
        db.close()
    return raw


def _token_used_at(raw_token: str):
    """Return the used_at value for the token row matching ``raw_token``."""
    from app.auth import hash_reset_token
    from app.database import SessionLocal
    from app.models import PasswordResetToken
    from sqlalchemy import select

    h = hash_reset_token(raw_token)
    db = SessionLocal()
    try:
        prt = db.scalar(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == h)
        )
        return prt.used_at if prt is not None else None
    finally:
        db.close()


# ===========================================================================
# Password-reset request: POST /api/auth/forgot-password
# (returns 204 for both known and unknown emails to prevent enumeration)
# ===========================================================================
def test_cov_forgot_password_known_email_creates_token(client):
    """A known, active email returns 204 and a reset-token row is created.

    Covers the real-account branch: generate token, persist, audit, queue the
    background email. The email task is a no-op against the disabled backend.
    """
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")  # call as anonymous

    res = client.post("/api/auth/forgot-password",
                      json={"email": BOOTSTRAP_EMAIL})
    assert res.status_code == 204

    # Confirm the token row was actually persisted.
    from app.database import SessionLocal
    from app.models import PasswordResetToken
    from sqlalchemy import select
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(PasswordResetToken).where(PasswordResetToken.user_id == uid)
        ).all()
    finally:
        db.close()
    assert len(rows) >= 1


def test_cov_forgot_password_unknown_email_204_default(client):
    """An unknown address gets the same 204 as a known one, so callers can't
    probe which emails are registered."""
    res = client.post("/api/auth/forgot-password",
                      json={"email": "no-such-person@example.com"})
    assert res.status_code == 204


# ===========================================================================
# Password-reset completion: POST /api/auth/reset-password
# ===========================================================================
def test_cov_reset_password_valid_token_succeeds(client):
    """A fresh, unexpired token resets the password: 204, token is marked used,
    new password authenticates, old password no longer does.

    Covers the happy path: expiry and used checks pass, password hash,
    session_version bump, token marked used, session rows wiped.
    """
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")
    raw = _insert_reset_token(uid)

    new_pw = "FreshReset123"
    res = client.post("/api/auth/reset-password",
                      json={"token": raw, "new_password": new_pw})
    assert res.status_code == 204, res.text

    assert _token_used_at(raw) is not None  # token is single-use

    ok = client.post("/api/auth/login",
                     json={"email": BOOTSTRAP_EMAIL, "password": new_pw})
    assert ok.status_code == 200
    bad = client.post("/api/auth/login",
                      json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD})
    assert bad.status_code == 401  # old password no longer works


def test_cov_reset_password_expired_token_rejected(client):
    """An expired token is rejected with 400 (the expires < now branch)."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")
    raw = _insert_reset_token(uid, expires_in_hours=-1.0)  # already expired

    res = client.post("/api/auth/reset-password",
                      json={"token": raw, "new_password": "AfterExpiry123"})
    assert res.status_code == 400
    assert "expired" in res.json()["detail"].lower()


def test_cov_reset_password_used_token_rejected(client):
    """An already-used token is rejected with 400 (the used_at is not None branch)."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")
    raw = _insert_reset_token(uid, used=True)

    res = client.post("/api/auth/reset-password",
                      json={"token": raw, "new_password": "AfterUsed1234"})
    assert res.status_code == 400
    assert "invalid or expired" in res.json()["detail"].lower()


def test_cov_reset_password_unknown_token_rejected(client):
    """A token that hashes to no stored row is rejected with 400 (prt is None guard)."""
    res = client.post("/api/auth/reset-password",
                      json={"token": "this-token-was-never-issued-xyz",
                            "new_password": "NeverMinted123"})
    assert res.status_code == 400
    assert "invalid or expired" in res.json()["detail"].lower()


def test_cov_reset_password_inactive_user_rejected(client, admin_client):
    """A valid token whose user has since been deactivated is rejected with 400
    (the not user.is_active branch)."""
    created = admin_client.post("/api/users", json={
        "name": "Resettable", "email": "resettable@test.local",
        "role": "user", "password": "Resettable9",
    })
    assert created.status_code == 201, created.text
    uid = created.json()["id"]

    raw = _insert_reset_token(uid)

    deact = admin_client.put(f"/api/users/{uid}", json={"is_active": False})
    assert deact.status_code == 200

    admin_client.post("/api/auth/logout")
    res = admin_client.post("/api/auth/reset-password",
                            json={"token": raw, "new_password": "Inactive1234"})
    assert res.status_code == 400
    assert "invalid or expired" in res.json()["detail"].lower()


def test_cov_reset_password_naive_expiry_is_handled(client):
    """A tz-naive expires_at (a SQLite round-trip artifact) must be coerced to
    UTC before comparison rather than raising. We write a naive future timestamp
    directly and confirm the reset still succeeds."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")

    from app.auth import generate_reset_token
    from app.database import SessionLocal
    from app.models import PasswordResetToken

    raw, token_hash = generate_reset_token()
    naive_future = datetime.utcnow() + timedelta(hours=2)  # deliberately tz-naive
    db = SessionLocal()
    try:
        db.add(PasswordResetToken(
            user_id=uid, token_hash=token_hash,
            expires_at=naive_future, used_at=None,
        ))
        db.commit()
    finally:
        db.close()

    res = client.post("/api/auth/reset-password",
                      json={"token": raw, "new_password": "NaiveExpiry12"})
    assert res.status_code == 204, res.text


# ===========================================================================
# Login edge cases — routes/auth.py
# ===========================================================================
def test_cov_login_wrong_password_401(client):
    """Wrong password for a real account returns a unified 401."""
    res = client.post("/api/auth/login", json={
        "email": BOOTSTRAP_EMAIL, "password": "definitely-the-wrong-one-9",
    })
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password"


def test_cov_login_inactive_user_blocked(client, admin_client):
    """A deactivated account cannot log in even with the correct password.
    Returns the same unified 401 to avoid leaking account state."""
    created = admin_client.post("/api/users", json={
        "name": "Switched Off", "email": "switchedoff@test.local",
        "role": "user", "password": "SwitchedOff9",
    })
    assert created.status_code == 201, created.text
    uid = created.json()["id"]
    off = admin_client.put(f"/api/users/{uid}", json={"is_active": False})
    assert off.status_code == 200
    admin_client.post("/api/auth/logout")

    res = admin_client.post("/api/auth/login", json={
        "email": "switchedoff@test.local", "password": "SwitchedOff9",
    })
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password"


def test_cov_login_lockout_returns_429(client):
    """When the per-account lockout bucket is full, the login route returns 429
    before reaching bcrypt verify. The bucket is pre-filled directly so the
    result is deterministic and independent of the IP limiter."""
    from app import account_lockout
    account_lockout._reset_for_tests()
    try:
        for _ in range(account_lockout._LOGIN_FAIL_LIMIT):
            account_lockout.record_failure(BOOTSTRAP_EMAIL)
        # Even the correct password must be rejected once the bucket is locked.
        res = client.post("/api/auth/login", json={
            "email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD,
        })
        assert res.status_code == 429
        assert res.headers.get("Retry-After")
    finally:
        account_lockout._reset_for_tests()


# ===========================================================================
# logout branches
# ===========================================================================
def test_cov_logout_token_user_deleted_skips_audit(client, admin_client):
    """If the user behind the cookie no longer exists, logout skips the audit
    write but still returns 204 and removes the jti session row.

    A fresh user logs in, the admin deletes that user (cascading their session
    rows), then we call logout with the orphaned cookie."""
    created = admin_client.post("/api/users", json={
        "name": "Ephemeral", "email": "ephemeral@test.local",
        "role": "user", "password": "Ephemeral9",
    })
    assert created.status_code == 201, created.text
    uid = created.json()["id"]

    # Second TestClient so the admin session cookie is undisturbed.
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as user_c:
        login = user_c.post("/api/auth/login", json={
            "email": "ephemeral@test.local", "password": "Ephemeral9",
        })
        assert login.status_code == 200

        # Delete the user via admin: removes the User row and cascades session
        # rows, so the subsequent logout carries a token whose user_id is gone.
        dele = admin_client.delete(f"/api/users/{uid}")
        assert dele.status_code in (200, 204), dele.text

        out = user_c.post("/api/auth/logout")
        assert out.status_code == 204  # idempotent regardless


def test_cov_logout_legacy_cookie_without_jti(client):
    """A signed legacy cookie with no jti hits the ``if jti:`` false branch:
    the audit row is written but no per-session delete runs. We craft a valid
    2-part token for the admin and present it as the session cookie."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")

    from app.auth import COOKIE_NAME, make_session_token
    legacy = make_session_token(uid, 0)  # no jti — produces a 2-part token

    res = client.post("/api/auth/logout", cookies={COOKIE_NAME: legacy})
    assert res.status_code == 204


def test_cov_logout_without_cookie_is_idempotent(client):
    """No session cookie: logout parses nothing, skips the DB block, returns 204.
    Logout is always idempotent."""
    res = client.post("/api/auth/logout")
    assert res.status_code == 204


# ===========================================================================
# _client_ip
# ===========================================================================
def test_cov_client_ip_trust_enabled_empty_xff_falls_through(monkeypatch):
    """TRUST_PROXY_FORWARDED_FOR=True with an empty X-Forwarded-For header:
    _client_ip skips the XFF branch and falls back to the transport client host.
    Uses a synthetic Request to stay hermetic and independent of the TestClient."""
    import app.config as config
    monkeypatch.setattr(config.Settings, "TRUST_PROXY_FORWARDED_FOR", True)

    from app.routes.auth import _client_ip
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "POST", "path": "/api/auth/login",
        "headers": [(b"x-forwarded-for", b"")],  # present but empty
        "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": ("10.20.30.40", 12345),
        "raw_path": b"/api/auth/login",
    }
    assert _client_ip(Request(scope)) == "10.20.30.40"


def test_cov_client_ip_trust_enabled_uses_rightmost_xff(monkeypatch):
    """With TRUST_PROXY_FORWARDED_FOR=True and a populated X-Forwarded-For,
    _client_ip returns the rightmost (proxy-appended) entry. The leftmost entry
    is attacker-controlled and must never be used for the audit/session IP."""
    import app.config as config
    monkeypatch.setattr(config.Settings, "TRUST_PROXY_FORWARDED_FOR", True)
    monkeypatch.setattr(config.Settings, "TRUST_PROXY_HOP_COUNT", 1)

    from app.routes.auth import _client_ip
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "POST", "path": "/api/auth/login",
        # attacker-forged, real proxy-appended peer
        "headers": [(b"x-forwarded-for", b"198.51.100.7, 10.0.0.1")],
        "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": ("10.0.0.1", 555),
        "raw_path": b"/api/auth/login",
    }
    assert _client_ip(Request(scope)) == "10.0.0.1"


def test_cov_client_ip_trust_enabled_garbage_xff_falls_through(monkeypatch):
    """A non-IP X-Forwarded-For value can't poison the session/audit IP:
    ip_address() raises ValueError, the except swallows it, and _client_ip
    falls back to the real transport client host."""
    import app.config as config
    monkeypatch.setattr(config.Settings, "TRUST_PROXY_FORWARDED_FOR", True)

    from app.routes.auth import _client_ip
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "POST", "path": "/api/auth/login",
        "headers": [(b"x-forwarded-for", b"not-an-ip-address")],  # not a valid IP
        "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": ("10.20.30.40", 12345),
        "raw_path": b"/api/auth/login",
    }
    assert _client_ip(Request(scope)) == "10.20.30.40"


def test_cov_client_ip_no_client_returns_empty(monkeypatch):
    """No transport client on the request: _client_ip returns "".
    TRUST_PROXY_FORWARDED_FOR stays at its default False."""
    from app.routes.auth import _client_ip
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "POST", "path": "/api/auth/login",
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": None,  # absent transport client
        "raw_path": b"/api/auth/login",
    }
    assert _client_ip(Request(scope)) == ""


# ===========================================================================
# get_current_user via the HTTP layer
# ===========================================================================
def test_cov_me_missing_cookie_401(client):
    """No session cookie: _user_from_request returns None and get_current_user
    raises 401."""
    res = client.get("/api/auth/me")
    assert res.status_code == 401
    assert res.json()["detail"] == "Not authenticated"


def test_cov_me_invalid_signature_401(client):
    """A cookie that fails the signature check resolves to None
    (parse_session_token raises BadSignature) and returns 401."""
    from app.auth import COOKIE_NAME
    res = client.get("/api/auth/me",
                     cookies={COOKIE_NAME: "not.a.validly.signed.token"})
    assert res.status_code == 401


def test_cov_me_expired_token_401(client):
    """A correctly-signed token whose timestamp is older than SESSION_TTL_SECONDS
    is rejected (SignatureExpired, a BadSignature subclass) and returns 401.

    Rather than wait a real day, we mint the cookie with a signer that backdates
    its own timestamp well past the TTL, using the same secret and salt as
    _signer(). No real time elapses."""
    import time as _time

    uid = _admin_user_id(client)
    client.post("/api/auth/logout")

    from app.auth import COOKIE_NAME, _signer
    from itsdangerous import TimestampSigner

    live = _signer()  # same secret + salt the app validates with

    class _OldSigner(TimestampSigner):
        def get_timestamp(self) -> int:  # returns a timestamp far in the past
            return int(_time.time()) - 200_000

    old = _OldSigner(live.secret_keys[-1], salt=live.salt)
    expired = old.sign(f"{uid}:0".encode("utf-8")).decode("utf-8")

    res = client.get("/api/auth/me", cookies={COOKIE_NAME: expired})
    assert res.status_code == 401


# ===========================================================================
# parse_session_token — non-UTF-8 payload
# ===========================================================================
def test_cov_parse_session_token_non_utf8_payload_returns_none(client):
    """A signed payload that isn't valid UTF-8: parse_session_token swallows the
    UnicodeDecodeError and returns None. We sign genuine non-UTF-8 bytes with
    the live signer and pass the signed bytes straight in (decoding to str first
    would fail before even reaching the function). itsdangerous returns the
    original bytes from unsign(); the raw.decode('utf-8') call is what raises."""
    from app import auth
    bad_bytes = b"\xff\xfe\x00bad"  # invalid UTF-8
    signed = auth._signer().sign(bad_bytes)  # keep as bytes
    assert auth.parse_session_token(signed) is None


# ===========================================================================
# Session-row validation (driven through a real authenticated request)
# ===========================================================================
def test_cov_expired_session_row_deletes_and_rejects(client):
    """An authenticated cookie whose backing session row has expired is rejected
    by _validate_session_row and the stale row is deleted inline.

    We log in normally (creating a real jti row), back-date its expires_at, then
    confirm the next /me returns 401 and the row is gone.
    """
    uid = _admin_user_id(client)

    from app.database import SessionLocal
    from app.models import Session as SessionRow
    from sqlalchemy import select

    db = SessionLocal()
    try:
        sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        assert sess is not None
        jti = sess.jti
        sess.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)  # back-dated
        db.commit()
    finally:
        db.close()

    # Cookie is still validly signed and session_version matches, so validation
    # reaches the expiry check, finds the row expired, deletes it, and rejects.
    res = client.get("/api/auth/me")
    assert res.status_code == 401

    db = SessionLocal()
    try:
        gone = db.scalar(select(SessionRow).where(SessionRow.jti == jti))
    finally:
        db.close()
    assert gone is None, "expired session row should have been deleted in-line"


def test_cov_naive_session_expiry_is_coerced(client):
    """A session row whose expires_at is tz-naive but still in the future must
    be coerced to UTC and accepted rather than crashing. /me must succeed."""
    uid = _admin_user_id(client)

    from app.database import SessionLocal
    from app.models import Session as SessionRow
    from sqlalchemy import select

    db = SessionLocal()
    try:
        sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        assert sess is not None
        sess.expires_at = datetime.utcnow() + timedelta(hours=2)  # tz-naive future
        # Back-date last_seen_at so the throttled bump path runs on this request.
        sess.last_seen_at = datetime.utcnow() - timedelta(minutes=10)
        db.commit()
    finally:
        db.close()

    res = client.get("/api/auth/me")
    assert res.status_code == 200


def test_cov_last_seen_bump_runs_after_throttle_window(client):
    """Once the throttle window has elapsed, _maybe_bump_last_seen writes a
    fresh last_seen_at. We back-date the field far enough that the next
    authenticated request triggers a bump, then confirm the value moved forward."""
    uid = _admin_user_id(client)

    from app.database import SessionLocal
    from app.models import Session as SessionRow
    from sqlalchemy import select

    old = datetime.now(timezone.utc) - timedelta(hours=1)
    db = SessionLocal()
    try:
        sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        assert sess is not None
        sess.last_seen_at = old
        db.commit()
    finally:
        db.close()

    res = client.get("/api/auth/me")
    assert res.status_code == 200

    db = SessionLocal()
    try:
        sess2 = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        bumped = sess2.last_seen_at
    finally:
        db.close()
    bumped_aware = bumped if bumped.tzinfo else bumped.replace(tzinfo=timezone.utc)
    assert bumped_aware > old, "last_seen_at should have been bumped forward"


# ---------------------------------------------------------------------------
# Direct-call tests for the session-row helpers. These cover the tz-aware arms
# and SQLAlchemyError handlers the HTTP path can't reach on SQLite: SQLite
# returns DateTime(timezone=True) columns tz-naive on read (so round-tripped
# rows always take the naive branch), and a healthy in-memory DB never makes
# commit() raise. We set datetime fields in-memory (aware values survive) and
# monkeypatch commit() for the error paths.
# ---------------------------------------------------------------------------
def _persisted_session_row(client):
    """Log in and return (uid, jti, db session, SessionRow) so helper calls
    operate on a genuine persisted row."""
    uid = _admin_user_id(client)
    from app.database import SessionLocal
    from app.models import Session as SessionRow
    from sqlalchemy import select
    db = SessionLocal()
    sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
    assert sess is not None
    return uid, sess.jti, db, sess


def test_cov_validate_session_row_aware_expiry_branch(client):
    """_validate_session_row with a tz-aware, future expires_at skips naive
    coercion and returns True. The in-memory attribute is set directly to an
    aware datetime to avoid a SQLite round-trip."""
    from app.auth import _validate_session_row
    from app.models import User
    uid, jti, db, sess = _persisted_session_row(client)
    try:
        sess.expires_at = datetime.now(timezone.utc) + timedelta(hours=2)  # aware
        sess.last_seen_at = datetime.now(timezone.utc)  # recent, bump skipped
        user = db.get(User, uid)
        assert _validate_session_row(db, jti, user) is True
    finally:
        db.close()


def test_cov_maybe_bump_last_seen_aware_recent_is_noop(client):
    """_maybe_bump_last_seen with an aware, recent last_seen_at skips naive
    coercion and early-returns at the throttle guard without writing."""
    from app.auth import _maybe_bump_last_seen
    uid, jti, db, sess = _persisted_session_row(client)
    try:
        now = datetime.now(timezone.utc)
        sess.last_seen_at = now  # aware, within throttle window
        before = sess.last_seen_at
        _maybe_bump_last_seen(db, sess, now, jti)
        assert sess.last_seen_at == before  # unchanged (throttled)
    finally:
        db.close()


def test_cov_delete_expired_session_commit_error_is_swallowed(client, monkeypatch):
    """If db.commit() raises SQLAlchemyError during a delete, _delete_expired_session
    logs and rolls back rather than propagating. commit is patched to raise;
    rollback must be called exactly once and no exception must escape."""
    from sqlalchemy.exc import SQLAlchemyError
    from app.auth import _delete_expired_session
    uid, jti, db, sess = _persisted_session_row(client)
    try:
        rolled_back = {"n": 0}
        real_rollback = db.rollback

        def _boom_commit():
            raise SQLAlchemyError("simulated commit failure")

        def _count_rollback():
            rolled_back["n"] += 1
            return real_rollback()

        monkeypatch.setattr(db, "commit", _boom_commit)
        monkeypatch.setattr(db, "rollback", _count_rollback)
        _delete_expired_session(db, sess, jti)  # must not raise
        assert rolled_back["n"] == 1
    finally:
        db.close()


def test_cov_maybe_bump_last_seen_commit_error_is_swallowed(client, monkeypatch):
    """If db.commit() raises while bumping last_seen_at, _maybe_bump_last_seen
    logs and rolls back. last_seen_at is set old enough to pass the throttle
    guard before commit is made to raise."""
    from sqlalchemy.exc import SQLAlchemyError
    from app.auth import _maybe_bump_last_seen
    uid, jti, db, sess = _persisted_session_row(client)
    try:
        rolled_back = {"n": 0}
        real_rollback = db.rollback

        def _boom_commit():
            raise SQLAlchemyError("simulated commit failure")

        def _count_rollback():
            rolled_back["n"] += 1
            return real_rollback()

        now = datetime.now(timezone.utc)
        sess.last_seen_at = now - timedelta(hours=1)  # aware, past the throttle window
        monkeypatch.setattr(db, "commit", _boom_commit)
        monkeypatch.setattr(db, "rollback", _count_rollback)
        _maybe_bump_last_seen(db, sess, now, jti)  # must not raise
        assert rolled_back["n"] == 1
    finally:
        db.close()


# ===========================================================================
# get_current_user_optional
# ===========================================================================
def test_cov_get_current_user_optional_paths(client):
    """get_current_user_optional returns None for an anonymous request and the
    User for an authenticated one. Both arms are covered with a synthetic
    Request and a real DB session."""
    uid = _admin_user_id(client)

    from app.auth import (
        COOKIE_NAME, get_current_user_optional, make_session_token,
    )
    from app.database import SessionLocal
    from app.models import Session as SessionRow
    from sqlalchemy import select
    from starlette.requests import Request

    def _req(cookie_header: bytes | None) -> Request:
        headers = [(b"cookie", cookie_header)] if cookie_header else []
        return Request({
            "type": "http", "method": "GET", "path": "/api/auth/me",
            "headers": headers, "query_string": b"", "scheme": "http",
            "server": ("testserver", 80), "client": ("test", 0),
            "raw_path": b"/api/auth/me",
        })

    db = SessionLocal()
    try:
        assert get_current_user_optional(_req(None), db) is None  # anonymous

        # Rebuild a valid cookie from the real session row so the jti resolves.
        sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        assert sess is not None
        tok = make_session_token(uid, 0, jti=sess.jti)
        user = get_current_user_optional(_req(f"{COOKIE_NAME}={tok}".encode()), db)
        assert user is not None and user.id == uid
    finally:
        db.close()


# ===========================================================================
# require_admin / require_manager_or_admin — 403 for low roles
# ===========================================================================
def test_cov_require_admin_forbids_regular_user(user_client):
    """A regular user on an admin-only endpoint gets 403. /api/sessions is
    admin-only."""
    res = user_client.get("/api/sessions")
    assert res.status_code == 403


def test_cov_require_admin_allows_admin(admin_client):
    """An admin passes require_admin."""
    res = admin_client.get("/api/sessions")
    assert res.status_code == 200


def test_cov_require_admin_unit_raises_for_manager():
    """require_admin raises 403 for a manager, tested with a direct call (no
    HTTP session needed)."""
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.auth import require_admin

    with pytest.raises(HTTPException) as ei:
        require_admin(SimpleNamespace(role="manager"))
    assert ei.value.status_code == 403


def test_cov_require_manager_or_admin_forbids_user():
    """require_manager_or_admin raises 403 for a plain user and passes through
    for manager or admin."""
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.auth import require_manager_or_admin

    with pytest.raises(HTTPException) as ei:
        require_manager_or_admin(SimpleNamespace(role="user"))
    assert ei.value.status_code == 403

    mgr = SimpleNamespace(role="manager")
    assert require_manager_or_admin(mgr) is mgr
    adm = SimpleNamespace(role="admin")
    assert require_manager_or_admin(adm) is adm
