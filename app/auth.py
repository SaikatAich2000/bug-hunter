"""Authentication primitives: bcrypt hashing, signed session cookies
(HttpOnly + SameSite=Lax), reset tokens, and role-check dependencies.

Token payload is `user_id:session_version:jti`, encrypted so the client cannot read the
user id — bumping session_version logs out every device; revoking a jti row logs out one.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.agile.integrity import set_actor
from app.config import env_str, get_settings
from app.database import get_db
from app.models import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    PasswordResetToken,
    User,
)
from app.models import (
    Session as SessionRow,
)

logger = logging.getLogger("bug_hunter.auth")

COOKIE_NAME = "bh_session"
HOST_COOKIE_NAME = "__Host-bh_session"


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


# bcrypt cost for new hashes: 12. The test suite lowers it with BCRYPT_TEST_ROUNDS so CI
# doesn't spend its time hashing; the override is read per call (tests set it after some app
# modules are imported), capped at 12, and ignored in production.
_MAX_BCRYPT_ROUNDS = 12


def _bcrypt_rounds() -> int:
    """Cost factor for new hashes; a test-only override never applies in production."""
    if get_settings().is_production:
        return _MAX_BCRYPT_ROUNDS
    try:
        return max(4, min(int(env_str("BCRYPT_TEST_ROUNDS", "") or _MAX_BCRYPT_ROUNDS), _MAX_BCRYPT_ROUNDS))
    except ValueError:
        return _MAX_BCRYPT_ROUNDS


def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt."""
    if not plain:
        raise ValueError("Password cannot be empty")
    # bcrypt caps input at 72 bytes -> sha256 pre-hash; must match verify_password forever.
    pre = hashlib.sha256(plain.encode("utf-8")).digest()
    return bcrypt.hashpw(pre, bcrypt.gensalt(rounds=_bcrypt_rounds())).decode("utf-8")


def verify_password(plain: str, hashed: Optional[str]) -> bool:
    """Constant-time check of a plaintext password against a stored hash."""
    if not hashed or not plain:
        return False
    pre = hashlib.sha256(plain.encode("utf-8")).digest()
    try:
        return bcrypt.checkpw(pre, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _signer() -> TimestampSigner:
    s = get_settings().SESSION_SECRET or _FALLBACK_SECRET
    return TimestampSigner(s, salt="bh-session-v2")


def _sealer() -> Fernet:
    """Encrypts the cookie payload so the user id is not readable by the client."""
    return _sealer_for(get_settings().SESSION_SECRET or _FALLBACK_SECRET)


@lru_cache(maxsize=4)
def _sealer_for(secret: str) -> Fernet:
    """Key derivation and Fernet setup run once per secret, not on every request."""
    key = hashlib.sha256(b"bh-session-v3:" + secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def make_session_token(user_id: int, session_version: int, jti: str) -> str:
    """Encrypted, authenticated token of user id, session version and session id (jti)."""
    return _sealer().encrypt(f"{user_id}:{session_version}:{jti}".encode("utf-8")).decode("utf-8")


def _open_session_token(token: str) -> Optional[bytes]:
    ttl = get_settings().SESSION_TTL_SECONDS
    try:
        data = token if isinstance(token, bytes) else token.encode("utf-8")
        return _sealer().decrypt(data, ttl=ttl)
    except (InvalidToken, UnicodeEncodeError):
        pass
    # Cookies issued before the payload was encrypted (signed, readable) stay valid
    # until they expire, so an upgrade does not log everyone out. Drop after one release.
    try:
        return _signer().unsign(token, max_age=ttl)
    except BadSignature:
        return None


def parse_session_token(token: str) -> Optional[tuple[int, int, str]]:
    """Verify a session cookie; return (user_id, session_version, jti) or None. A cookie
    without a session id (from before per-device sessions) can't be revoked, so it is refused."""
    if not token:
        return None
    raw = _open_session_token(token)
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return None
    parts = text.split(":")
    if len(parts) != 3 or not parts[2]:
        return None
    try:
        return int(parts[0]), int(parts[1]), parts[2]
    except ValueError:
        return None


def new_jti() -> str:
    """Random opaque session ID (192 bits)."""
    return secrets.token_urlsafe(24)


def _cookie_secure(settings) -> bool:
    """Secure flag: COOKIE_SECURE, or derived from an https APP_BASE_URL."""
    return bool(settings.COOKIE_SECURE) or settings.APP_BASE_URL.lower().startswith("https://")


def session_cookie_name(settings=None) -> str:
    """``__Host-`` pins the cookie to this exact host over HTTPS (no Domain, Path=/); browsers
    reject the prefix without Secure, so plain-HTTP dev keeps the unprefixed name."""
    return HOST_COOKIE_NAME if _cookie_secure(settings or get_settings()) else COOKIE_NAME


def session_cookie_value(request: Request) -> str:
    """The session token from the request: the current name first, then the pre-prefix
    name that cookies issued before the upgrade still use."""
    return request.cookies.get(HOST_COOKIE_NAME) or request.cookies.get(COOKIE_NAME) or ""


def set_session_cookie(response: Response, user: User, jti: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=session_cookie_name(settings),
        value=make_session_token(user.id, user.session_version or 0, jti=jti),
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=_cookie_secure(settings),
        samesite="lax",
        path="/",
    )
    if _cookie_secure(settings):
        # A pre-upgrade cookie must not shadow or outlive the new one.
        response.delete_cookie(key=COOKIE_NAME, path="/", httponly=True, secure=True, samesite="lax")


def clear_session_cookie(response: Response) -> None:
    # Mirror the set attributes; some browsers key the delete on samesite/secure.
    settings = get_settings()
    secure = _cookie_secure(settings)
    names = (COOKIE_NAME, HOST_COOKIE_NAME) if secure else (COOKIE_NAME,)
    for name in names:
        response.delete_cookie(key=name, path="/", httponly=True, secure=secure, samesite="lax")


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


# Throttle last_seen_at writes; per-request updates would be a hot write.
_LAST_SEEN_THROTTLE_SECONDS = get_settings().LAST_SEEN_THROTTLE_SECONDS


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


def _session_row_usable(db: Session, sess: Optional[SessionRow], jti: str, user: User) -> bool:
    """True iff the fetched session row belongs to this user and has not expired; also
    prunes an expired row and refreshes last_seen_at."""
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


def _validate_session_row(db: Session, jti: str, user: User) -> bool:
    """True iff the jti's session row is valid for this user."""
    return _session_row_usable(db, db.scalar(select(SessionRow).where(SessionRow.jti == jti)), jti, user)


def _user_from_request(request: Request, db: Session) -> Optional[User]:
    parsed = parse_session_token(session_cookie_value(request))
    if parsed is None:
        return None
    user_id, session_version, jti = parsed
    # One round trip for the user and the session row it names (was two queries).
    row = db.execute(
        select(User, SessionRow)
        .outerjoin(SessionRow, (SessionRow.user_id == User.id) & (SessionRow.jti == jti))
        .where(User.id == user_id)
    ).first()
    if row is None:
        return None
    user, sess = row
    if not user.is_active:
        return None
    # session_version bump (password change/forced logout) invalidates old cookies.
    if (user.session_version or 0) != session_version:
        return None
    if not _session_row_usable(db, sess, jti, user):
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
    # The route receives this same session, so work-item history written by
    # its flushes is attributed to this user (app/agile/integrity.py).
    set_actor(db, user.id)
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_manager_or_admin(user: User = Depends(get_current_user)) -> User:
    if user.role not in (ROLE_ADMIN, ROLE_MANAGER):
        raise HTTPException(status_code=403, detail="Manager or admin access required")
    return user


def can_edit_bug(user: User, item_type: str = "Bug") -> bool:
    """Anyone may edit Bugs; Tasks/Requirements are admin/manager only."""
    if item_type in ("Task", "Requirement"):
        return user.role in (ROLE_ADMIN, ROLE_MANAGER)
    return True


def can_delete_bug(user: User) -> bool:
    """Work-item deletion is admin-only for every type."""
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
