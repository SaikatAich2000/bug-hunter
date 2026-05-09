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
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    ROLE_USER,
    PasswordResetToken,
    Session as SessionRow,
    User,
    VALID_ROLES,
)

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
    # bcrypt has a 72-byte input limit. Pre-hash with sha256 to handle long
    # passwords without surprising the user, then base64 the digest so it
    # fits comfortably under the limit.
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
    except (SignatureExpired, BadSignature):
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
    response.delete_cookie(key=COOKIE_NAME, path="/")


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


def _user_from_request(request: Request, db: Session) -> Optional[User]:
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id, session_version, jti = parsed
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    # Token's session_version must match the user's current — bump on
    # password change / reset / forced logout.
    if (user.session_version or 0) != session_version:
        return None

    # Per-session revocation: if the cookie carries a jti, look it up.
    # Missing or expired row → token rejected. Legacy tokens (no jti)
    # pre-date the sessions table, so we accept them without a row.
    if jti is not None:
        sess = db.scalar(select(SessionRow).where(SessionRow.jti == jti))
        if sess is None or sess.user_id != user.id:
            return None
        now = datetime.now(timezone.utc)
        expires = sess.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < now:
            # Cleanly remove expired rows so the admin list doesn't get
            # cluttered. A single delete-on-touch is cheaper than a
            # background sweeper.
            try:
                db.delete(sess)
                db.commit()
            except Exception:
                db.rollback()
            return None
        # Throttled last_seen update — only writes once per minute per
        # session, which keeps the request hot path cheap on busy users.
        last_seen = sess.last_seen_at
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if (now - last_seen).total_seconds() >= _LAST_SEEN_THROTTLE_SECONDS:
            try:
                sess.last_seen_at = now
                db.commit()
            except Exception:
                db.rollback()
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


def can_edit_bug(user: User, bug_reporter_id: Optional[int], assignee_ids: list[int]) -> bool:
    """Centralised rule: every authenticated, active user can edit any bug.

    This was tightened to "reporter / assignee / manager / admin" in earlier
    builds, but the v3.1 product spec relaxed it: a regular user can now
    edit any bug and reassign it to anyone. Deletion is the only bug
    operation still restricted (admin-only — see can_delete_bug)."""
    return True


def can_delete_bug(user: User) -> bool:
    """Bug deletion is admin-only across all roles. The original code
    allowed managers too; the v3.1 spec moved this to admin-only so a
    bug — once filed — can't be erased by anyone except the very top of
    the hierarchy. Reporters / assignees / managers all still have full
    edit rights, just not delete."""
    return user.role == ROLE_ADMIN


def can_manage_projects(user: User) -> bool:
    """Create / edit projects: admin or manager. Delete is admin-only —
    see can_delete_project."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_delete_project(user: User) -> bool:
    return user.role == ROLE_ADMIN


def can_manage_users(user: User) -> bool:
    """Create / edit users: admin or manager (per v3.1 spec). Previously
    admin-only. Delete is still admin-only — see can_delete_user."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_delete_user(user: User) -> bool:
    return user.role == ROLE_ADMIN


def can_view_audit(user: User) -> bool:
    """Audit trail is hidden from regular users per v3.1 spec — they don't
    need to see who did what across the system."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_manage_sessions(user: User) -> bool:
    """Only admins can list / revoke other users' sessions."""
    return user.role == ROLE_ADMIN
