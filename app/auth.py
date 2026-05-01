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
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, Response, status
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
# In production, set SESSION_SECRET in .env so it survives restarts.
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
    return TimestampSigner(s, salt="bh-session-v1")


def make_session_token(user_id: int) -> str:
    """Return a signed token containing the user id."""
    return _signer().sign(str(user_id).encode("utf-8")).decode("utf-8")


def parse_session_token(token: str) -> Optional[int]:
    """Verify a session cookie and return the user id, or None if invalid/expired."""
    if not token:
        return None
    try:
        raw = _signer().unsign(token, max_age=get_settings().SESSION_TTL_SECONDS)
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    try:
        return int(raw.decode("utf-8"))
    except (ValueError, AttributeError):
        return None


def set_session_cookie(response: Response, user_id: int) -> None:
    settings = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=make_session_token(user_id),
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


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
def _user_from_request(request: Request, db: Session) -> Optional[User]:
    token = request.cookies.get(COOKIE_NAME, "")
    user_id = parse_session_token(token)
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
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
