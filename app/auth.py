"""Authentication primitives.

Responsibilities:
  - Hash + verify passwords (bcrypt).
  - Sign + verify session cookies (itsdangerous).
  - Generate + verify password-reset tokens.
  - Provide FastAPI dependencies that resolve the current user from
    the session cookie, with role-based access checks.
  - Track every active session in the DB (jti) so admins can list and
    revoke individual sessions.

Cookies are used rather than bearer tokens: HTTP-only cookies can't be read
by JS, so an XSS payload can't exfiltrate the session. The cookie is
`SameSite=Lax`, which blocks cross-site POST/PUT/DELETE from third-party
origins.

Token payload format:
  `user_id:session_version[:jti]`
  - `jti` is the server-side session ID. Tokens issued before this existed
    don't have one; they are accepted but don't appear in the admin session
    list until the user logs in again.

Session-version invalidation (global):
  Each session token carries the user's `session_version`. Changing or
  resetting a password bumps that integer in the DB, so every previously
  issued cookie fails validation on the next request, logging out every
  device for that user.

Per-session revocation:
  When an admin revokes a session row from the `sessions` table, the matching
  jti no longer resolves, so just that one device is logged out. Other
  sessions for the same user are untouched.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    PasswordResetToken,
    Session as SessionRow,
    User,
)

logger = logging.getLogger("bug_hunter.auth")

COOKIE_NAME = "bh_session"


def trusted_forwarded_ip(xff: str, hops: int) -> Optional[str]:
    """Pick the trustworthy client address from an X-Forwarded-For chain.

    A reverse proxy appends the peer it saw to the right of any client-supplied
    list, so with N trusted proxies the trustworthy entry is the Nth from the
    right. The left-most entry is fully client-controlled and must not be used
    for a security/rate-limit/audit decision. Returns a validated IP string, or
    None when the chosen entry is missing or not a real IP (caller falls back to
    the transport peer). Shared by the rate limiter (app/main.py) and the
    session/audit IP resolver (app/routes/auth.py) so the two can't diverge.
    """
    parts = [p.strip() for p in (xff or "").split(",") if p.strip()]
    if not parts:
        return None
    candidate = parts[-min(hops, len(parts))]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate

# Process-local fallback so dev works without setting SESSION_SECRET.
# In production, set SESSION_SECRET in .env so it survives restarts AND
# is shared across multi-worker uvicorn deployments.
_FALLBACK_SECRET = secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt. Returns a string suitable for DB."""
    if not plain:
        raise ValueError("Password cannot be empty")
    # bcrypt caps input at 72 bytes. Pre-hashing with sha256 handles long
    # passwords: the 32-byte digest is safely under the cap and passed as raw
    # bytes (pyca/bcrypt uses the buffer length, not NUL termination).
    # This must stay identical to verify_password; changing it would invalidate
    # every stored hash.
    pre = hashlib.sha256(plain.encode("utf-8")).digest()
    return bcrypt.hashpw(pre, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: Optional[str]) -> bool:
    """Constant-time check of a plaintext password against a stored hash."""
    if not hashed or not plain:
        return False
    pre = hashlib.sha256(plain.encode("utf-8")).digest()
    try:
        return bcrypt.checkpw(pre, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------
def _signer() -> TimestampSigner:
    s = get_settings().SESSION_SECRET or _FALLBACK_SECRET
    return TimestampSigner(s, salt="bh-session-v2")


def make_session_token(user_id: int, session_version: int = 0, jti: str | None = None) -> str:
    """Return a signed token containing the user id, session version, and
    optional per-session jti. The jti lets admins revoke an individual session
    without bumping session_version (which logs out every device)."""
    if jti:
        payload = f"{user_id}:{session_version}:{jti}"
    else:
        payload = f"{user_id}:{session_version}"
    return _signer().sign(payload.encode("utf-8")).decode("utf-8")


def parse_session_token(token: str) -> Optional[tuple[int, int, Optional[str]]]:
    """Verify a session cookie and return (user_id, session_version, jti),
    or None if invalid/expired/malformed.

    `jti` is None for 2-part tokens issued before the sessions table existed;
    those cookies still authenticate but aren't visible in the admin
    session-list screen.
    """
    if not token:
        return None
    try:
        raw = _signer().unsign(token, max_age=get_settings().SESSION_TTL_SECONDS)
    except BadSignature:
        return None
    try:
        text = raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return None
    parts = text.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]), int(parts[1]), parts[2] or None
        if len(parts) == 2:
            return int(parts[0]), int(parts[1]), None
        # Single-int cookies issued before the version was added: accept so a
        # deploy doesn't log everyone out.
        if len(parts) == 1:
            return int(parts[0]), 0, None
        return None
    except ValueError:
        return None


def new_jti() -> str:
    """Random opaque session ID. 192 bits is plenty for collision resistance
    even at billions of concurrent sessions."""
    return secrets.token_urlsafe(24)


def _cookie_secure(settings) -> bool:
    """Whether to set the Secure flag on the session cookie.

    Honours COOKIE_SECURE, and also derives Secure from an https APP_BASE_URL
    so a deploy that serves over TLS but didn't set COOKIE_SECURE=true still
    gets a Secure cookie. Stays False for the http localhost default so local
    runs and the test client keep working.
    """
    return bool(settings.COOKIE_SECURE) or settings.APP_BASE_URL.lower().startswith("https://")


def set_session_cookie(response: Response, user: User, jti: str | None = None) -> None:
    settings = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=make_session_token(user.id, user.session_version or 0, jti=jti),
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=_cookie_secure(settings),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    # Mirror the attributes the cookie was set with so every browser reliably
    # matches and clears it (some browsers key the delete on samesite/secure).
    settings = get_settings()
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        secure=_cookie_secure(settings),
        samesite="lax",
    )


# ---------------------------------------------------------------------------
# Password-reset tokens
# ---------------------------------------------------------------------------
PASSWORD_RESET_TTL = timedelta(hours=2)


def generate_reset_token() -> tuple[str, str]:
    """Return (plaintext_token, sha256_hex_hash). Email the plaintext, store the hash."""
    raw = secrets.token_urlsafe(32)
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw, h


def hash_reset_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def invalidate_outstanding_reset_tokens(db: Session, user_id: int) -> int:
    """Mark every still-unused reset token for this user as used. Called on
    successful password change/reset so old email links can't be replayed.
    Returns the number of tokens invalidated (for audit logging).

    Uses a single guarded UPDATE (set used_at WHERE used_at IS NULL) rather
    than load-then-set in Python, so it's atomic under concurrency."""
    now = datetime.now(timezone.utc)
    return (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
        .update({PasswordResetToken.used_at: now}, synchronize_session=False)
    )


def purge_consumed_reset_tokens(db: Session) -> int:
    """Delete reset tokens that are expired or already used. The table is never
    otherwise pruned; called opportunistically when a new reset is requested so
    it can't grow without bound. Returns the number of rows removed."""
    now = datetime.now(timezone.utc)
    return (
        db.query(PasswordResetToken)
        .filter(
            (PasswordResetToken.used_at.isnot(None))
            | (PasswordResetToken.expires_at < now)
        )
        .delete(synchronize_session=False)
    )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
# How often we touch `last_seen_at` on each authenticated request. Updating
# it on every request would be a hot write; throttling to once per minute
# is precise enough for the admin "active sessions" view.
_LAST_SEEN_THROTTLE_SECONDS = 60


def _delete_expired_session(db: Session, sess: SessionRow, jti: str) -> None:
    """Drop an expired session row on a request-path read; errors are logged,
    not raised."""
    try:
        db.delete(sess)
        db.commit()
    except SQLAlchemyError:
        logger.exception("Failed to delete expired session jti=%s", jti)
        db.rollback()


def _maybe_bump_last_seen(db: Session, sess: SessionRow, now: datetime, jti: str) -> None:
    """Throttled write of sess.last_seen_at, skipped if recent."""
    last_seen = sess.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if (now - last_seen).total_seconds() < _LAST_SEEN_THROTTLE_SECONDS:
        return
    try:
        sess.last_seen_at = now
        db.commit()
    except SQLAlchemyError:
        logger.exception("Failed to bump last_seen_at for session jti=%s", jti)
        db.rollback()


def _validate_session_row(db: Session, jti: str, user: User) -> bool:
    """Return True iff the session row for jti is valid for this user.

    Also: deletes expired rows in-line and refreshes last_seen_at when
    enough time has passed. Returns False to signal the caller to reject
    the request.
    """
    sess = db.scalar(select(SessionRow).where(SessionRow.jti == jti))
    if sess is None or sess.user_id != user.id:
        return False
    now = datetime.now(timezone.utc)
    expires = sess.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        _delete_expired_session(db, sess, jti)
        return False
    _maybe_bump_last_seen(db, sess, now, jti)
    return True


def _user_from_request(request: Request, db: Session) -> Optional[User]:
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id, session_version, jti = parsed
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    # session_version is bumped on password change/reset/forced logout, which
    # invalidates every previously issued cookie for this user.
    if (user.session_version or 0) != session_version:
        return None
    # If the cookie has a jti, validate the session row. Tokens without a jti
    # pre-date the sessions table.
    if jti is None:
        if get_settings().SESSION_REQUIRE_JTI:
            # Refuse cookies that can't be revoked per-device.
            # Operators flip this on after a migration window.
            logger.info("Rejected jti-less session for user_id=%s", user_id)
            return None
        logger.debug("Accepting jti-less session for user_id=%s", user_id)
        return user
    if not _validate_session_row(db, jti, user):
        return None
    return user


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Require any active, logged-in user. 401 otherwise."""
    user = _user_from_request(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Return the current user if logged in, else None. Never raises."""
    return _user_from_request(request, db)


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_manager_or_admin(user: User = Depends(get_current_user)) -> User:
    if user.role not in (ROLE_ADMIN, ROLE_MANAGER):
        raise HTTPException(status_code=403, detail="Manager or admin access required")
    return user


def can_edit_bug(
    user: User,
    bug_reporter_id: Optional[int],
    assignee_ids: list[int],
    item_type: str = "Bug",
) -> bool:
    """Whether `user` may edit a work item.

    Per-type rules:
      - Bug:         every authenticated user can edit.
      - Requirement: only admin or manager. Users are read-only here.
      - Task:        only admin or manager. Users are read-only here.
    """
    del bug_reporter_id, assignee_ids
    if item_type in ("Task", "Requirement"):
        return user.role in (ROLE_ADMIN, ROLE_MANAGER)
    return True


def can_delete_bug(user: User, item_type: str = "Bug") -> bool:
    """Deletion is admin-only across every work-item type. Managers can edit,
    never delete. The item_type parameter exists for symmetry with
    can_edit_bug.
    """
    del item_type
    return user.role == ROLE_ADMIN


def can_edit_comment(user: User) -> bool:
    """Editing a comment is admin-only: comments are evidence and only admins
    curate them (uploaders/managers cannot rewrite them)."""
    return user.role == ROLE_ADMIN


def can_delete_comment(user: User) -> bool:
    """Deleting a comment is admin-only (see can_edit_comment)."""
    return user.role == ROLE_ADMIN


def can_delete_attachment(user: User) -> bool:
    """Deleting an attachment (bug- or comment-level) is admin-only: admins
    curate the evidence; uploaders and managers cannot remove their own files."""
    return user.role == ROLE_ADMIN


def can_edit_event(user: User) -> bool:
    """Events are admin/manager only; users have no edit rights."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_delete_event(user: User) -> bool:
    """Event delete is admin-only (managers can edit but not delete)."""
    return user.role == ROLE_ADMIN


def can_manage_projects(user: User) -> bool:
    """Create / edit projects: admin or manager. Delete is admin-only
    (see can_delete_project)."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_delete_project(user: User) -> bool:
    return user.role == ROLE_ADMIN


def can_manage_users(user: User) -> bool:
    """Create / edit users: admin or manager. Delete is admin-only (see
    can_delete_user)."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_delete_user(user: User) -> bool:
    return user.role == ROLE_ADMIN


def can_view_audit(user: User) -> bool:
    """Audit trail is hidden from regular users; they don't need to see who
    did what across the system."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_manage_sessions(user: User) -> bool:
    """Only admins can list / revoke other users' sessions."""
    return user.role == ROLE_ADMIN
