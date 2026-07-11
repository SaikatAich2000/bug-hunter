"""Authentication primitives: bcrypt hashing, signed session cookies
(HttpOnly + SameSite=Lax), reset tokens, and role-check dependencies.

Token payload is `user_id:session_version[:jti]` — bumping session_version
logs out every device; revoking a jti row logs out one device.
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
    """Pick the client IP from X-Forwarded-For: with N trusted proxies, only the
    Nth-from-the-right entry is trustworthy (left-most is client-controlled)."""
    parts = [p.strip() for p in (xff or "").split(",") if p.strip()]
    if not parts:
        return None
    candidate = parts[-min(hops, len(parts))]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate

# Dev-only fallback; production must set SESSION_SECRET (survives restarts, shared across workers).
_FALLBACK_SECRET = secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt."""
    if not plain:
        raise ValueError("Password cannot be empty")
    # bcrypt caps input at 72 bytes -> sha256 pre-hash; must match verify_password forever.
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
    """Signed token of user id, session version, and optional per-session jti."""
    if jti:
        payload = f"{user_id}:{session_version}:{jti}"
    else:
        payload = f"{user_id}:{session_version}"
    return _signer().sign(payload.encode("utf-8")).decode("utf-8")


def parse_session_token(token: str) -> Optional[tuple[int, int, Optional[str]]]:
    """Verify a session cookie; return (user_id, session_version, jti) or None.
    jti is None for legacy pre-sessions-table tokens."""
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
        # Accept legacy single-int cookies so a deploy doesn't log everyone out.
        if len(parts) == 1:
            return int(parts[0]), 0, None
        return None
    except ValueError:
        return None


def new_jti() -> str:
    """Random opaque session ID (192 bits)."""
    return secrets.token_urlsafe(24)


def _cookie_secure(settings) -> bool:
    """Secure flag: COOKIE_SECURE, or derived from an https APP_BASE_URL."""
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
    # Mirror the set attributes; some browsers key the delete on samesite/secure.
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
    """Return (plaintext_token, sha256_hex). Email the plaintext, store the hash."""
    raw = secrets.token_urlsafe(32)
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw, h


def hash_reset_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def invalidate_outstanding_reset_tokens(db: Session, user_id: int) -> int:
    """Mark the user's unused reset tokens as used (atomic guarded UPDATE) so
    old email links can't be replayed. Returns the count for audit logging."""
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
    """Delete expired/used reset tokens (only pruning path for this table)."""
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
# Throttle last_seen_at writes; per-request updates would be a hot write.
_LAST_SEEN_THROTTLE_SECONDS = 60


def _delete_expired_session(db: Session, sess: SessionRow, jti: str) -> None:
    """Drop an expired session row; errors are logged, not raised."""
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
    """True iff the jti's session row is valid for this user; also prunes
    expired rows and refreshes last_seen_at."""
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
    # session_version bump (password change/forced logout) invalidates old cookies.
    if (user.session_version or 0) != session_version:
        return None
    # jti-less tokens pre-date the sessions table.
    if jti is None:
        if get_settings().SESSION_REQUIRE_JTI:
            # Refuse cookies that can't be revoked per-device.
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
    """Anyone may edit Bugs; Tasks/Requirements are admin/manager only."""
    del bug_reporter_id, assignee_ids
    if item_type in ("Task", "Requirement"):
        return user.role in (ROLE_ADMIN, ROLE_MANAGER)
    return True


def can_delete_bug(user: User, item_type: str = "Bug") -> bool:
    """Work-item deletion is admin-only for every type."""
    del item_type
    return user.role == ROLE_ADMIN


def can_edit_comment(user: User) -> bool:
    """Admin-only: comments are evidence; only admins curate them."""
    return user.role == ROLE_ADMIN


def can_delete_comment(user: User) -> bool:
    """Deleting a comment is admin-only (see can_edit_comment)."""
    return user.role == ROLE_ADMIN


def can_delete_attachment(user: User) -> bool:
    """Attachment deletion is admin-only; uploaders can't remove their own files."""
    return user.role == ROLE_ADMIN


def can_edit_event(user: User) -> bool:
    """Events are admin/manager only; users have no edit rights."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_delete_event(user: User) -> bool:
    """Event delete is admin-only (managers can edit but not delete)."""
    return user.role == ROLE_ADMIN


def can_manage_projects(user: User) -> bool:
    """Create/edit projects: admin or manager (delete is admin-only)."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_delete_project(user: User) -> bool:
    return user.role == ROLE_ADMIN


def can_manage_users(user: User) -> bool:
    """Create/edit users: admin or manager (delete is admin-only)."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_delete_user(user: User) -> bool:
    return user.role == ROLE_ADMIN


def can_view_audit(user: User) -> bool:
    """Audit trail is admin/manager only."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_manage_sessions(user: User) -> bool:
    """Only admins can list / revoke other users' sessions."""
    return user.role == ROLE_ADMIN
