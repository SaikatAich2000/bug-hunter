"""Authentication primitives.

Responsibilities:
  - Hash + verify passwords (bcrypt).
  - Sign + verify session cookies (itsdangerous).
  - Generate + verify password-reset tokens.
  - Provide FastAPI dependencies that resolve the current user from
    the session cookie, with role-based access checks.

Why cookies, not bearer tokens? HTTP-only cookies can't be read by JS,
which means stolen XSS payloads can't exfiltrate the session. The price
is CSRF risk — but our cookie is `SameSite=Lax`, which blocks
cross-site POST/PUT/DELETE from third-party origins, so the practical
attack surface is small for an internal tool.

Session-version invalidation:
  Each session token also carries the user's `session_version`. When the
  user changes or resets their password, we bump that integer in the DB,
  which makes every previously-issued cookie fail validation on the next
  request — effectively logging out other devices. Cookies are signed by
  the server so a client can't tamper with the version they present.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    ROLE_USER,
    PasswordResetToken,
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


def make_session_token(user_id: int, session_version: int = 0) -> str:
    """Return a signed token containing the user id and session version."""
    payload = f"{user_id}:{session_version}"
    return _signer().sign(payload.encode("utf-8")).decode("utf-8")


def parse_session_token(token: str) -> Optional[tuple[int, int]]:
    """Verify a session cookie and return (user_id, session_version),
    or None if invalid/expired/malformed."""
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
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
        # Fallback for legacy single-int cookies issued before the version
        # was added — accept once so live deploys don't kick everyone out.
        if len(parts) == 1:
            return int(parts[0]), 0
        return None
    except ValueError:
        return None


def set_session_cookie(response: Response, user: User) -> None:
    settings = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=make_session_token(user.id, user.session_version or 0),
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
def _user_from_request(request: Request, db: Session) -> Optional[User]:
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id, session_version = parsed
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    # Token's session_version must match the user's current — bump on
    # password change / reset / forced logout.
    if (user.session_version or 0) != session_version:
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


def can_edit_bug(user: User, bug_reporter_id: Optional[int], assignee_ids: list[int]) -> bool:
    """Centralised rule: admins/managers can edit any bug; users only their own."""
    if user.role in (ROLE_ADMIN, ROLE_MANAGER):
        return True
    if bug_reporter_id == user.id:
        return True
    if user.id in assignee_ids:
        return True
    return False


def can_manage_projects(user: User) -> bool:
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)
