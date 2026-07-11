"""Authentication endpoints: login, logout, and password management."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import account_lockout
from app.auth import (
    PASSWORD_RESET_TTL,
    clear_session_cookie,
    generate_reset_token,
    get_current_user,
    hash_password,
    hash_reset_token,
    invalidate_outstanding_reset_tokens,
    new_jti,
    purge_consumed_reset_tokens,
    set_session_cookie,
    trusted_forwarded_ip,
    verify_password,
)
from app.password_breach import is_password_breached
from app.config import get_settings
from app.database import get_db
from app.email_service import notify_password_reset
from app.models import Activity, PasswordResetToken, Session as SessionRow, User
from app.schemas import (
    ChangePasswordIn,
    ForgotPasswordIn,
    LoginIn,
    MeOut,
    ResetPasswordIn,
)

logger = logging.getLogger("bug_hunter.auth")

# Verified for unknown emails so timing can't distinguish "no account" from "wrong password".
_DUMMY_PASSWORD_HASH = hash_password("dummy-not-a-real-credential")

router = APIRouter(prefix="/api/auth", tags=["auth"])


_DETAIL_INVALID_RESET_TOKEN = "Invalid or expired reset token"

def _audit(db: Session, actor: User | None, action: str, detail: str, entity_id: int | None = None) -> None:
    db.add(Activity(
        bug_id=None, entity_type="auth", entity_id=entity_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action, detail=detail,
    ))


def _reject_if_breached(plain: str) -> None:
    """Reject an HIBP-breached password; fails open on network errors (see app/password_breach.py)."""
    if is_password_breached(plain):
        raise HTTPException(
            status_code=400,
            detail="This password appears in a known breach corpus. "
                   "Please choose a different one.",
        )


def _mask_email(email: str) -> str:
    """Mask email local part for logs (``alice@x.com`` -> ``a***@x.com``); avoids PII in log stores."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    if not local:
        return "@" + domain
    head = local[0]
    return f"{head}***@{domain}"


def _client_ip(request: Request) -> str:
    """Client IP for sessions/audit. X-Forwarded-For is honoured only when
    TRUST_PROXY_FORWARDED_FOR is set (spoofable otherwise); matches the rate limiter."""
    settings = get_settings()
    if settings.TRUST_PROXY_FORWARDED_FOR:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            # Right-most proxy-appended entry; the left-most is client-spoofable.
            ip = trusted_forwarded_ip(fwd, settings.TRUST_PROXY_HOP_COUNT)
            if ip is not None:
                return ip[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return ""


@router.post("/login", response_model=MeOut)
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> User:
    """Verify credentials, create a session row, and set the signed cookie.
    The cookie's `jti` maps back to the row, enabling per-session admin revocation."""
    # Check lockout before bcrypt so a login flood doesn't become a CPU flood.
    account_lockout.check_locked(payload.email)

    # LoginIn already lowercases the email.
    user = db.scalar(select(User).where(User.email == payload.email))
    # Run bcrypt even for unknown emails to keep response timing uniform.
    if user is None:
        verify_password(payload.password, _DUMMY_PASSWORD_HASH)
        password_ok = False
    else:
        password_ok = verify_password(payload.password, user.password_hash)
    # Same 401 for all failures so existence/disabled status doesn't leak.
    if user is None or not password_ok:
        account_lockout.record_failure(payload.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        logger.info("Login refused: inactive account %s", _mask_email(user.email))
        account_lockout.record_failure(payload.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Clear the lockout bucket so transient typos don't carry forward.
    account_lockout.clear(payload.email)

    settings = get_settings()
    jti = new_jti()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.SESSION_TTL_SECONDS)
    sess = SessionRow(
        user_id=user.id,
        jti=jti,
        user_agent=(request.headers.get("user-agent") or "")[:400],
        ip_address=_client_ip(request),
        expires_at=expires_at,
    )
    db.add(sess)

    set_session_cookie(response, user, jti=jti)
    _audit(db, user, "login", f"{user.email} logged in")
    db.commit()
    return user


@router.post("/logout", status_code=204)
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    """Clear the session cookie and its server-side row; always 204 (idempotent)."""
    from app.auth import COOKIE_NAME, parse_session_token
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed:
        user_id, _version, jti = parsed
        user = db.get(User, user_id)
        if user:
            _audit(db, user, "logout", f"{user.email} logged out")
        if jti:
            # Only this session; the user's other sessions remain.
            db.execute(
                SessionRow.__table__.delete().where(SessionRow.jti == jti)
            )
        db.commit()
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)) -> User:
    """Return the currently logged-in user."""
    return user


@router.post("/change-password", status_code=204)
def change_password(
    payload: ChangePasswordIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Change own password: bumps session_version (killing other sessions), invalidates
    outstanding reset tokens, and re-issues a fresh session so the caller stays logged in."""
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    # Reject same-password change: it would boot every other device for no gain.
    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(
            status_code=400,
            detail="New password must be different from the current password.",
        )

    _reject_if_breached(payload.new_password)

    user.password_hash = hash_password(payload.new_password)
    user.session_version = (user.session_version or 0) + 1
    invalidated = invalidate_outstanding_reset_tokens(db, user.id)

    # Rows are already invalid via the version bump; delete so the admin session list stays clean.
    db.execute(SessionRow.__table__.delete().where(SessionRow.user_id == user.id))

    # Fresh session for the current device so the user isn't bounced to login.
    settings = get_settings()
    jti = new_jti()
    new_sess = SessionRow(
        user_id=user.id,
        jti=jti,
        user_agent=(request.headers.get("user-agent") or "")[:400],
        ip_address=_client_ip(request),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=settings.SESSION_TTL_SECONDS),
    )
    db.add(new_sess)

    _audit(db, user, "password_changed",
           f"{user.email} changed their password"
           + (f" (invalidated {invalidated} outstanding reset link(s))" if invalidated else ""))
    db.commit()

    out = Response(status_code=204)
    set_session_cookie(out, user, jti=jti)
    return out


@router.post("/forgot-password", status_code=204)
def forgot_password(
    payload: ForgotPasswordIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Response:
    """Issue a password-reset email. With FORGOT_PASSWORD_ENUMERATION_SAFE (default)
    always 204 so account existence never leaks; otherwise 404s on unknown addresses."""
    settings = get_settings()
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not user.is_active:
        # Run (and discard) the same token work so timing doesn't reveal account existence.
        if settings.FORGOT_PASSWORD_ENUMERATION_SAFE:
            generate_reset_token()
        _audit(db, None, "password_reset_no_account",
               f"Password reset attempted for unknown/inactive email: {_mask_email(payload.email)}")
        db.commit()
        if settings.FORGOT_PASSWORD_ENUMERATION_SAFE:
            return Response(status_code=204)
        raise HTTPException(
            status_code=404,
            detail="We couldn't find an account with that email. Check the address or contact an administrator",
        )
    # Purge stale tokens inline; there's no background job for this table.
    purge_consumed_reset_tokens(db)
    raw_token, token_hash = generate_reset_token()
    prt = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + PASSWORD_RESET_TTL,
    )
    db.add(prt)
    _audit(db, None, "password_reset_requested",
           f"Password reset requested for {user.email}")
    db.commit()

    base = get_settings().APP_BASE_URL.rstrip("/")
    reset_url = f"{base}/reset.html?token={raw_token}"
    background.add_task(
        notify_password_reset, user.email, user.name, reset_url,
    )
    return Response(status_code=204)


@router.post("/reset-password", status_code=204)
def reset_password(payload: ResetPasswordIn, db: Session = Depends(get_db)) -> Response:
    """Set a new password via reset token; bumps session_version and
    invalidates the user's other outstanding reset tokens."""
    h = hash_reset_token(payload.token)
    prt = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == h))
    if prt is None:
        raise HTTPException(status_code=400, detail=_DETAIL_INVALID_RESET_TOKEN)
    now = datetime.now(timezone.utc)
    expires = prt.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        raise HTTPException(status_code=400, detail=_DETAIL_INVALID_RESET_TOKEN)

    user = db.get(User, prt.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail=_DETAIL_INVALID_RESET_TOKEN)

    # HIBP check after token validation so invalid-token callers can't probe the breach signal.
    _reject_if_breached(payload.new_password)

    # Guarded UPDATE consumes the token once: a racing request sees rowcount 0, closing replay.
    consumed = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.id == prt.id,
            PasswordResetToken.used_at.is_(None),
        )
        .update({PasswordResetToken.used_at: now}, synchronize_session=False)
    )
    if not consumed:
        db.rollback()
        raise HTTPException(status_code=400, detail=_DETAIL_INVALID_RESET_TOKEN)

    user.password_hash = hash_password(payload.new_password)
    user.session_version = (user.session_version or 0) + 1
    invalidated = invalidate_outstanding_reset_tokens(db, user.id)

    # Rows are already invalid via the version bump; delete so the admin session list stays clean.
    db.execute(SessionRow.__table__.delete().where(SessionRow.user_id == user.id))

    _audit(db, user, "password_reset",
           f"{user.email} reset their password via token"
           + (f" (invalidated {invalidated} other outstanding reset link(s))"
              if invalidated else ""))
    db.commit()
    return Response(status_code=204)
