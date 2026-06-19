"""Authentication primitives.

Responsibilities:
  - Hash + verify passwords (bcrypt).
  - Sign + verify session cookies (itsdangerous).
  - Generate + verify password-reset tokens.
  - Provide FastAPI dependencies that resolve the current user from
    the session cookie, with role-based access checks.
  - Track every active session in the DB (jti) so admins can list and
    revoke individual sessions Keycloak-style.

Why cookies, not bearer tokens? HTTP-only cookies can't be read by JS,
which means stolen XSS payloads can't exfiltrate the session. The price
is CSRF risk — but our cookie is `SameSite=Lax`, which blocks
cross-site POST/PUT/DELETE from third-party origins, so the practical
attack surface is small for an internal tool.

Token payload format:
  `user_id:session_version[:jti]`
  - `jti` is the unique server-side session ID. Tokens issued by older
    builds don't have one; we accept them (legacy mode) but they won't
    appear in the admin session list until the user logs in again.

Session-version invalidation (global, blunt):
  Each session token also carries the user's `session_version`. When the
  user changes or resets their password, we bump that integer in the DB,
  which makes every previously-issued cookie fail validation on the next
  request — effectively logging out every device for that user.

Per-session revocation (precise, Keycloak-style):
  When admin revokes a session row from the `sessions` table, the
  matching jti no longer resolves on validation, so just that one device
  is logged out. Other sessions for the same user are untouched.
"""
from __future__ import annotations

import hashlib
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
    # bcrypt caps input at 72 bytes, so pre-hash with sha256 to handle long
    # passwords without surprising the user. The 32-byte digest is well under
    # the cap and is passed as RAW bytes: the pinned pyca/bcrypt backend uses the
    # buffer's explicit length and does NOT NUL-truncate, so an embedded 0x00 is
    # safe. NOTE: this pre-processing must stay byte-for-byte identical to
    # verify_password — changing it (e.g. base64-encoding the digest) would
    # invalidate every password already stored.
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
    optional per-session jti. The jti is what lets admins revoke an
    individual session without bumping session_version (which would log
    out every device the user has)."""
    if jti:
        payload = f"{user_id}:{session_version}:{jti}"
    else:
        payload = f"{user_id}:{session_version}"
    return _signer().sign(payload.encode("utf-8")).decode("utf-8")


def parse_session_token(token: str) -> Optional[tuple[int, int, Optional[str]]]:
    """Verify a session cookie and return (user_id, session_version, jti),
    or None if invalid/expired/malformed.

    `jti` is None for legacy 2-part tokens issued before the sessions
    table existed — those cookies still authenticate, but they're not
    visible in the admin session-list screen.
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
        # Fallback for legacy single-int cookies issued before the version
        # was added — accept once so live deploys don't kick everyone out.
        if len(parts) == 1:
            return int(parts[0]), 0, None
        return None
    except ValueError:
        return None


def new_jti() -> str:
    """Random opaque session ID. 192 bits is plenty for collision resistance
    even at billions of concurrent sessions."""
    return secrets.token_urlsafe(24)


def set_session_cookie(response: Response, user: User, jti: str | None = None) -> None:
    settings = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=make_session_token(user.id, user.session_version or 0, jti=jti),
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    # Mirror the attributes the cookie was SET with so every browser reliably
    # matches and clears it (some browsers key the delete on samesite/secure).
    settings = get_settings()
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        secure=settings.COOKIE_SECURE,
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
    Returns the number of tokens invalidated (for audit logging)."""
    now = datetime.now(timezone.utc)
    rows = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None))
        .all()
    )
    for r in rows:
        r.used_at = now
    return len(rows)


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
    """Throttled write of sess.last_seen_at - skipped if recent."""
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
    # Token's session_version must match the user's current - bump on
    # password change / reset / forced logout.
    if (user.session_version or 0) != session_version:
        return None
    # Per-session revocation: if the cookie carries a jti, look it up.
    # Legacy tokens (no jti) pre-date the sessions table - accept them.
    if jti is not None and not _validate_session_row(db, jti, user):
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
    """Return the current user if logged in, else None — never raises."""
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


def can_edit_event(user: User) -> bool:
    """Events are admin/manager only — users have no edit rights."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_delete_event(user: User) -> bool:
    """Event delete is admin-only (managers can edit but not delete)."""
    return user.role == ROLE_ADMIN


def can_manage_projects(user: User) -> bool:
    """Create / edit projects: admin or manager. Delete is admin-only —
    see can_delete_project."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_delete_project(user: User) -> bool:
    return user.role == ROLE_ADMIN


def can_manage_users(user: User) -> bool:
    """Create / edit users: admin or manager. Delete is admin-only — see
    can_delete_user."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_delete_user(user: User) -> bool:
    return user.role == ROLE_ADMIN


def can_view_audit(user: User) -> bool:
    """Audit trail is hidden from regular users — they don't need to see who
    did what across the system."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_manage_sessions(user: User) -> bool:
    """Only admins can list / revoke other users' sessions."""
    return user.role == ROLE_ADMIN
