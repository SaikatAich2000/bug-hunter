"""Coverage slice: authentication routes (app/routes/auth.py) + auth helpers
(app/auth.py).

Why everything is imported INSIDE each test
-------------------------------------------
The ``client`` fixture (see conftest.py) deletes and re-imports every ``app.*``
module between tests so each test picks up its own env overrides. A module
captured at THIS file's top level therefore goes stale (points at an earlier
import generation) the moment any client-using test has run. Importing the
``app.*`` modules we need INSIDE the test body guarantees we touch the SAME
import generation the running app is using — the same discipline used by
tests/test_password_policy.py. To patch app.config we set the ``Settings``
CLASS attribute (module-level instances go stale), per the project rules.

The ``changeme`` password is intentionally always valid (a documented legacy
exception); these tests never assert it is rejected.

Everything is hermetic: no real network. The password-reset email backend is
disabled in tests, and the reset TOKEN is produced server-side
(app.auth.generate_reset_token → (raw, sha256_hash); only the hash is stored).
The reset-flow tests therefore insert PasswordResetToken rows DIRECTLY through
the app's own ``SessionLocal`` using ``generate_reset_token()`` so we hold the
raw token without ever reading an email — letting us deterministically build
valid / expired / already-used / unknown-token cases.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD


# ---------------------------------------------------------------------------
# Small helpers (kept local so re-imports never leave us with a stale module).
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
    """Create a PasswordResetToken row directly and return the RAW token.

    Mirrors exactly what forgot_password() stores (a sha256 hash of a random
    token), but lets the test hold the plaintext so it can drive reset-password
    without reading an email. expires_in_hours<0 yields an expired token; used
    stamps used_at so the already-used branch can be exercised.
    """
    # Import inside the test-call so we bind to the CURRENT app generation
    # (and therefore the test's SQLite engine), not a stale one.
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
# (enumeration-safe default returns 204 for both known and unknown emails)
# ===========================================================================
def test_cov_forgot_password_known_email_creates_token(client):
    """A known, active email returns 204 AND a reset-token row is persisted.

    Covers routes/auth.py:305-322 (the real-account branch: generate token,
    persist, audit, queue the background email). The email task is a no-op
    against the disabled backend, so this stays fully hermetic.
    """
    uid = _admin_user_id(client)
    # Hit the public endpoint as an anonymous caller.
    client.post("/api/auth/logout")

    res = client.post("/api/auth/forgot-password",
                      json={"email": BOOTSTRAP_EMAIL})
    assert res.status_code == 204

    # Exactly the side effect we care about: a token row now exists for admin.
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
    """Enumeration-safe default: an unknown address gets the SAME 204 as a
    known one (routes/auth.py:296-299), so callers can't probe which emails
    exist."""
    res = client.post("/api/auth/forgot-password",
                      json={"email": "no-such-person@example.com"})
    assert res.status_code == 204


# ===========================================================================
# Password-reset completion: POST /api/auth/reset-password
# Target: routes/auth.py 338-370 (validate token, expiry/used checks, set pw).
# ===========================================================================
def test_cov_reset_password_valid_token_succeeds(client):
    """A fresh, unexpired token resets the password: 204, the token is marked
    used, and the new password authenticates while the old one no longer does.

    Drives the whole happy path of reset_password (routes/auth.py:338-370):
    expiry+used checks pass, user lookup, password hash, session_version bump,
    token marked used, session rows wiped.
    """
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")
    raw = _insert_reset_token(uid)

    new_pw = "FreshReset123"
    res = client.post("/api/auth/reset-password",
                      json={"token": raw, "new_password": new_pw})
    assert res.status_code == 204, res.text

    # The token is now single-use spent.
    assert _token_used_at(raw) is not None

    # New password works; the bootstrap one no longer does.
    ok = client.post("/api/auth/login",
                     json={"email": BOOTSTRAP_EMAIL, "password": new_pw})
    assert ok.status_code == 200
    bad = client.post("/api/auth/login",
                      json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD})
    assert bad.status_code == 401


def test_cov_reset_password_expired_token_rejected(client):
    """An expired token is rejected with 400 (routes/auth.py:342-343 —
    ``expires < now`` branch)."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")
    raw = _insert_reset_token(uid, expires_in_hours=-1.0)  # already expired

    res = client.post("/api/auth/reset-password",
                      json={"token": raw, "new_password": "AfterExpiry123"})
    assert res.status_code == 400
    assert "expired" in res.json()["detail"].lower()


def test_cov_reset_password_used_token_rejected(client):
    """An already-used token is rejected with 400 (routes/auth.py:342-343 —
    ``used_at is not None`` branch)."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")
    raw = _insert_reset_token(uid, used=True)

    res = client.post("/api/auth/reset-password",
                      json={"token": raw, "new_password": "AfterUsed1234"})
    assert res.status_code == 400
    assert "invalid or expired" in res.json()["detail"].lower()


def test_cov_reset_password_unknown_token_rejected(client):
    """A token that hashes to no stored row is rejected with 400
    (routes/auth.py:336-337 — the ``prt is None`` guard, just above our
    target span)."""
    res = client.post("/api/auth/reset-password",
                      json={"token": "this-token-was-never-issued-xyz",
                            "new_password": "NeverMinted123"})
    assert res.status_code == 400
    assert "invalid or expired" in res.json()["detail"].lower()


def test_cov_reset_password_inactive_user_rejected(client, admin_client):
    """A valid token whose user has since been deactivated is rejected with
    400 (routes/auth.py:346-347 — ``not user.is_active`` branch)."""
    # Create a user via the admin session, then deactivate them.
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
    """If a stored token's expires_at is tz-naive (legacy/SQLite round-trip),
    reset_password must coerce it to UTC before comparing rather than raising
    (routes/auth.py:340-341). We write a naive future timestamp directly and
    confirm the reset still succeeds."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")

    from app.auth import generate_reset_token
    from app.database import SessionLocal
    from app.models import PasswordResetToken

    raw, token_hash = generate_reset_token()
    # Deliberately tz-NAIVE, well in the future.
    naive_future = datetime.utcnow() + timedelta(hours=2)
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
    """Real user, wrong password → unified 401 (routes/auth.py:149-154)."""
    res = client.post("/api/auth/login", json={
        "email": BOOTSTRAP_EMAIL, "password": "definitely-the-wrong-one-9",
    })
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password"


def test_cov_login_inactive_user_blocked(client, admin_client):
    """A deactivated account cannot log in even with the right password —
    same unified 401, no account-state leak (routes/auth.py:155-161)."""
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
    """When the per-account lockout bucket is already tripped, the login route
    short-circuits with 429 BEFORE the bcrypt verify (routes/auth.py:127 ->
    account_lockout.check_locked). We pre-fill the bucket via the lockout
    module (as test_security.py does) so the assertion is deterministic and
    independent of the IP limiter."""
    from app import account_lockout
    account_lockout._reset_for_tests()
    try:
        for _ in range(account_lockout._LOGIN_FAIL_LIMIT):
            account_lockout.record_failure(BOOTSTRAP_EMAIL)
        # Bucket is now locked; even the CORRECT password must 429.
        res = client.post("/api/auth/login", json={
            "email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD,
        })
        assert res.status_code == 429
        assert res.headers.get("Retry-After")
    finally:
        account_lockout._reset_for_tests()


# ===========================================================================
# routes/auth.py logout branches — 195->197, 197->203
# ===========================================================================
def test_cov_logout_token_user_deleted_skips_audit(client, admin_client):
    """logout() looks up the user behind the cookie; if that user no longer
    exists, the audit-write branch is skipped but logout still 204s and the
    session row keyed by jti is deleted (routes/auth.py:194-203, the
    ``if user:`` false edge → 195->197).

    We log in a fresh user, delete that user as admin (which cascades their
    session rows), then call logout carrying the now-orphaned cookie."""
    # Separate login client so its cookie is the one we orphan.
    created = admin_client.post("/api/users", json={
        "name": "Ephemeral", "email": "ephemeral@test.local",
        "role": "user", "password": "Ephemeral9",
    })
    assert created.status_code == 201, created.text
    uid = created.json()["id"]

    # Borrow a second TestClient against the same app so we don't disturb the
    # admin session cookie.
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as user_c:
        login = user_c.post("/api/auth/login", json={
            "email": "ephemeral@test.local", "password": "Ephemeral9",
        })
        assert login.status_code == 200

        # Delete the user via admin — removes the User row (and cascades the
        # session rows), so the logout below resolves a parsed token whose
        # user_id no longer maps to a User.
        dele = admin_client.delete(f"/api/users/{uid}")
        assert dele.status_code in (200, 204), dele.text

        out = user_c.post("/api/auth/logout")
        assert out.status_code == 204  # idempotent regardless


def test_cov_logout_legacy_cookie_without_jti(client):
    """A signed legacy cookie with NO jti exercises the ``if jti:`` false edge
    in logout (routes/auth.py:197->203): the user audit row IS written but no
    per-session delete runs. We craft a valid 2-part token for the admin and
    present it as the session cookie."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")

    from app.auth import COOKIE_NAME, make_session_token
    legacy = make_session_token(uid, 0)  # no jti → 2-part token

    res = client.post("/api/auth/logout", cookies={COOKIE_NAME: legacy})
    assert res.status_code == 204


def test_cov_logout_without_cookie_is_idempotent(client):
    """logout() with no session cookie parses nothing and skips the whole
    DB block, still returning 204 (routes/auth.py:192->204 — the ``if parsed:``
    false arc). Logout is deliberately idempotent."""
    res = client.post("/api/auth/logout")
    assert res.status_code == 204


# ===========================================================================
# routes/auth.py _client_ip — 110->114, 116
# ===========================================================================
def test_cov_client_ip_trust_enabled_empty_xff_falls_through(monkeypatch):
    """With TRUST_PROXY_FORWARDED_FOR=True but an EMPTY X-Forwarded-For header,
    _client_ip skips the XFF branch (110->114) and falls back to the transport
    client host. Exercised directly with a synthetic Request so it's hermetic
    and independent of the TestClient transport."""
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


def test_cov_client_ip_trust_enabled_uses_leftmost_xff(monkeypatch):
    """With TRUST_PROXY_FORWARDED_FOR=True and a populated X-Forwarded-For,
    _client_ip returns the leftmost (original-client) entry, truncated to 64
    chars (routes/auth.py:110-113)."""
    import app.config as config
    monkeypatch.setattr(config.Settings, "TRUST_PROXY_FORWARDED_FOR", True)

    from app.routes.auth import _client_ip
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "POST", "path": "/api/auth/login",
        "headers": [(b"x-forwarded-for", b"198.51.100.7, 10.0.0.1")],
        "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": ("10.0.0.1", 555),
        "raw_path": b"/api/auth/login",
    }
    assert _client_ip(Request(scope)) == "198.51.100.7"


def test_cov_client_ip_no_client_returns_empty(monkeypatch):
    """When the request has no transport client at all, _client_ip returns ""
    (routes/auth.py:116). TRUST gate left at default False so we go straight to
    the client check."""
    from app.routes.auth import _client_ip
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "POST", "path": "/api/auth/login",
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": None,  # no client
        "raw_path": b"/api/auth/login",
    }
    assert _client_ip(Request(scope)) == ""


# ===========================================================================
# app/auth.py — get_current_user via the HTTP layer
# ===========================================================================
def test_cov_me_missing_cookie_401(client):
    """No session cookie → get_current_user raises 401 (auth.py:293-297 via
    _user_from_request returning None on a missing cookie)."""
    res = client.get("/api/auth/me")
    assert res.status_code == 401
    assert res.json()["detail"] == "Not authenticated"


def test_cov_me_invalid_signature_401(client):
    """A cookie that fails the itsdangerous signature check resolves to None
    (auth.py parse_session_token → BadSignature) → 401."""
    from app.auth import COOKIE_NAME
    res = client.get("/api/auth/me",
                     cookies={COOKIE_NAME: "not.a.validly.signed.token"})
    assert res.status_code == 401


def test_cov_me_expired_token_401(client):
    """A correctly-signed token whose embedded timestamp is older than
    SESSION_TTL_SECONDS is rejected as expired by the TimestampSigner
    (auth.py parse_session_token → SignatureExpired, a BadSignature subclass)
    → 401.

    We can't sleep out a real day, so we mint the cookie with a signer that
    backdates its own timestamp ~2.3 days into the past, using the SAME secret
    + salt the app's _signer() uses. The live verifier (default 86400s TTL)
    then sees it as expired. Fully deterministic — no real time elapses."""
    import time as _time

    uid = _admin_user_id(client)
    client.post("/api/auth/logout")

    from app.auth import COOKIE_NAME, _signer
    from itsdangerous import TimestampSigner

    live = _signer()  # carries the exact secret + salt the app validates with

    class _OldSigner(TimestampSigner):
        def get_timestamp(self) -> int:  # backdate well past the TTL
            return int(_time.time()) - 200_000

    old = _OldSigner(live.secret_keys[-1], salt=live.salt)
    expired = old.sign(f"{uid}:0".encode("utf-8")).decode("utf-8")

    res = client.get("/api/auth/me", cookies={COOKIE_NAME: expired})
    assert res.status_code == 401


# ===========================================================================
# app/auth.py parse_session_token — 131-132 (raw.decode raises)
# ===========================================================================
def test_cov_parse_session_token_non_utf8_payload_returns_none(client):
    """If the signed payload unsigns to bytes that aren't valid UTF-8,
    parse_session_token swallows the UnicodeDecodeError and returns None
    (auth.py:130-132). The signature check must PASS first, so we sign genuine
    non-UTF-8 bytes with the live signer and feed the signed BYTES straight in
    (decoding the token to str would itself fail and never reach the function).
    itsdangerous re-returns the original bytes from unsign(), and it's the
    subsequent raw.decode('utf-8') that raises — exactly the guarded branch."""
    from app import auth
    bad_bytes = b"\xff\xfe\x00bad"  # invalid UTF-8 once unsigned
    signed = auth._signer().sign(bad_bytes)  # keep as bytes
    assert auth.parse_session_token(signed) is None


# ===========================================================================
# app/auth.py session-row validation — 223-228, 234->236, 238-243, 258->260,
# 261-262  (driven through a real authenticated request)
# ===========================================================================
def test_cov_expired_session_row_deletes_and_rejects(client):
    """An authenticated cookie whose backing session row has expired must be
    rejected (auth.py _validate_session_row 258->260, 261-262) AND the stale
    row deleted in-line (_delete_expired_session, auth.py:223-228).

    We log in normally (creating a real jti session row), then back-date that
    row's expires_at into the past via the DB. The next /me must 401, and the
    row must be gone afterwards.
    """
    uid = _admin_user_id(client)

    # Find the freshly-created session row's jti.
    from app.database import SessionLocal
    from app.models import Session as SessionRow
    from sqlalchemy import select

    db = SessionLocal()
    try:
        sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        assert sess is not None
        jti = sess.jti
        # Back-date expiry so the row reads as expired (tz-aware past).
        sess.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
    finally:
        db.close()

    # The cookie is still validly signed and the session_version matches, so
    # validation reaches the row's expiry check, finds it expired, deletes it,
    # and rejects the request.
    res = client.get("/api/auth/me")
    assert res.status_code == 401

    db = SessionLocal()
    try:
        gone = db.scalar(select(SessionRow).where(SessionRow.jti == jti))
    finally:
        db.close()
    assert gone is None, "expired session row should have been deleted in-line"


def test_cov_naive_session_expiry_is_coerced(client):
    """A session row whose expires_at is tz-NAIVE but still in the future must
    be coerced to UTC and accepted (auth.py:258-259, the naive→aware branch),
    not crash the request. /me must succeed."""
    uid = _admin_user_id(client)

    from app.database import SessionLocal
    from app.models import Session as SessionRow
    from sqlalchemy import select

    db = SessionLocal()
    try:
        sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        assert sess is not None
        # tz-naive future expiry.
        sess.expires_at = datetime.utcnow() + timedelta(hours=2)
        # Also force last_seen_at well into the past so the throttled bump
        # path (auth.py:236-240) actually runs on this request.
        sess.last_seen_at = datetime.utcnow() - timedelta(minutes=10)
        db.commit()
    finally:
        db.close()

    res = client.get("/api/auth/me")
    assert res.status_code == 200


def test_cov_last_seen_bump_runs_after_throttle_window(client):
    """When more than the throttle window has elapsed, _maybe_bump_last_seen
    writes a fresh last_seen_at (auth.py:238-240). We back-date last_seen_at
    (tz-aware) far enough that the next authenticated request bumps it, and
    assert it moved forward."""
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
# Direct-call coverage for the session-row helpers. These exercise the
# tz-AWARE arms (234->236, 258->260) and the SQLAlchemyError handlers
# (226-228, 241-243) that the HTTP path can't reach on SQLite: the SQLite
# dialect returns DateTime(timezone=True) columns as tz-NAIVE on read, so a
# round-tripped row always takes the naive branch, and a healthy in-memory DB
# never makes commit() raise. We build SessionRow objects in memory (aware
# datetimes survive) and, for the error paths, wrap a real session whose
# commit() is monkeypatched to raise. Fully hermetic — no real failure needed.
# ---------------------------------------------------------------------------
def _persisted_session_row(client):
    """Log in, then return (uid, jti, real SessionRow attached to a fresh
    session) so helper calls operate on a genuine, persisted row."""
    uid = _admin_user_id(client)
    from app.database import SessionLocal
    from app.models import Session as SessionRow
    from sqlalchemy import select
    db = SessionLocal()
    sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
    assert sess is not None
    return uid, sess.jti, db, sess


def test_cov_validate_session_row_aware_expiry_branch(client):
    """_validate_session_row with a tz-AWARE, future expires_at takes the
    258->260 arc (skips the naive coercion) and returns True. We set the
    in-memory attribute to an aware datetime so it isn't a SQLite round-trip."""
    from app.auth import _validate_session_row
    from app.models import User
    uid, jti, db, sess = _persisted_session_row(client)
    try:
        sess.expires_at = datetime.now(timezone.utc) + timedelta(hours=2)  # aware
        sess.last_seen_at = datetime.now(timezone.utc)  # recent → bump skipped
        user = db.get(User, uid)
        assert _validate_session_row(db, jti, user) is True
    finally:
        db.close()


def test_cov_maybe_bump_last_seen_aware_recent_is_noop(client):
    """_maybe_bump_last_seen with an AWARE, recent last_seen_at takes the
    234->236 arc (skips the naive coercion) and then early-returns at the
    throttle guard (236) without writing."""
    from app.auth import _maybe_bump_last_seen
    uid, jti, db, sess = _persisted_session_row(client)
    try:
        now = datetime.now(timezone.utc)
        sess.last_seen_at = now  # aware + within throttle window
        before = sess.last_seen_at
        _maybe_bump_last_seen(db, sess, now, jti)
        assert sess.last_seen_at == before  # unchanged (throttled)
    finally:
        db.close()


def test_cov_delete_expired_session_commit_error_is_swallowed(client, monkeypatch):
    """If db.commit() raises SQLAlchemyError mid-delete, _delete_expired_session
    logs and rolls back rather than propagating (auth.py:226-228). We patch
    commit on the live session to raise; rollback must be called and no
    exception escapes."""
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
        # Must not raise.
        _delete_expired_session(db, sess, jti)
        assert rolled_back["n"] == 1
    finally:
        db.close()


def test_cov_maybe_bump_last_seen_commit_error_is_swallowed(client, monkeypatch):
    """If db.commit() raises while bumping last_seen_at, _maybe_bump_last_seen
    logs and rolls back (auth.py:241-243). We force last_seen_at old enough to
    pass the throttle guard, then make commit raise."""
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
        sess.last_seen_at = now - timedelta(hours=1)  # aware + past throttle
        monkeypatch.setattr(db, "commit", _boom_commit)
        monkeypatch.setattr(db, "rollback", _count_rollback)
        _maybe_bump_last_seen(db, sess, now, jti)  # must not raise
        assert rolled_back["n"] == 1
    finally:
        db.close()


# ===========================================================================
# app/auth.py get_current_user_optional — 306
# ===========================================================================
def test_cov_get_current_user_optional_paths(client):
    """get_current_user_optional returns None for an anonymous request and the
    User for an authenticated one (auth.py:306). Exercised directly with a
    synthetic Request + a real DB session so both return arms run."""
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
        # Anonymous → None.
        assert get_current_user_optional(_req(None), db) is None

        # Authenticated: rebuild a valid cookie from the real session row so
        # the jti resolves.
        sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        assert sess is not None
        tok = make_session_token(uid, 0, jti=sess.jti)
        user = get_current_user_optional(_req(f"{COOKIE_NAME}={tok}".encode()), db)
        assert user is not None and user.id == uid
    finally:
        db.close()


# ===========================================================================
# app/auth.py require_admin / require_manager_or_admin — 403 for low roles
# ===========================================================================
def test_cov_require_admin_forbids_regular_user(user_client):
    """A regular user hitting an admin-only endpoint gets 403 — require_admin
    raises (auth.py:309-312). /api/sessions is admin-only."""
    res = user_client.get("/api/sessions")
    assert res.status_code == 403


def test_cov_require_admin_allows_admin(admin_client):
    """Sanity counterpart: an admin passes require_admin (the True arm of
    auth.py:309-312)."""
    res = admin_client.get("/api/sessions")
    assert res.status_code == 200


def test_cov_require_admin_unit_raises_for_manager():
    """Direct unit call: require_admin raises 403 for a manager (covers the
    raise in auth.py:310-311 without needing a manager HTTP session)."""
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.auth import require_admin

    with pytest.raises(HTTPException) as ei:
        require_admin(SimpleNamespace(role="manager"))
    assert ei.value.status_code == 403


def test_cov_require_manager_or_admin_forbids_user():
    """require_manager_or_admin raises 403 for a plain user (auth.py:315-318)
    and returns the user for manager/admin."""
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
