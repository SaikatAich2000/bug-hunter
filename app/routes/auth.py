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

# Always run bcrypt verify, even when no user matches the email, so response
# timing can't distinguish "account not found" from "wrong password".
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
    """Reject a password that appears in the HIBP corpus.

    Called from every code path that sets a password. On a network error it
    allows the password through, so an HIBP outage doesn't block legitimate
    password changes; see app/password_breach.py.
    """
    if is_password_breached(plain):
        raise HTTPException(
            status_code=400,
            detail="This password appears in a known breach corpus. "
                   "Please choose a different one.",
        )


def _mask_email(email: str) -> str:
    """Mask the local part of an email for safe inclusion in logs.

    Log stores often have broader access controls than the app DB, so writing
    raw emails there leaks PII; the masked form stays useful for diagnosis.
    ``alice@example.com`` becomes ``a***@example.com``.
    """
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    if not local:
        return "@" + domain
    head = local[0]
    return f"{head}***@{domain}"


def _client_ip(request: Request) -> str:
    """Client IP for the session log and audit trail.

    Only honours X-Forwarded-For when TRUST_PROXY_FORWARDED_FOR is set;
    otherwise a client could spoof the header and forge the recorded IP.
    Matches the rate-limit middleware's ``_client_ip`` in app/main.py.
    """
    settings = get_settings()
    if settings.TRUST_PROXY_FORWARDED_FOR:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            # Take the right-most (proxy-appended) entry, not the client-supplied
            # left-most one, which could be spoofed to poison the session list /
            # audit trail. trusted_forwarded_ip also validates it parses as a real
            # IP. Matches the rate limiter.
            ip = trusted_forwarded_ip(fwd, settings.TRUST_PROXY_HOP_COUNT)
            if ip is not None:
                return ip[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return ""


@router.post("/login", response_model=MeOut)
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> User:
    """Verify credentials, create a server-side session row, and set the
    signed cookie. The cookie carries a `jti` that maps back to the row,
    which is what makes per-session admin revocation possible."""
    # Check lockout before bcrypt so a login flood doesn't turn into a CPU flood.
    account_lockout.check_locked(payload.email)

    # LoginIn already lowercases the email.
    user = db.scalar(select(User).where(User.email == payload.email))
    # Run bcrypt either way; the dummy hash never matches, so password_ok
    # stays False for unknown emails while keeping response timing uniform.
    if user is None:
        verify_password(payload.password, _DUMMY_PASSWORD_HASH)
        password_ok = False
    else:
        password_ok = verify_password(payload.password, user.password_hash)
    # Same 401 for all failure modes so the response never leaks whether the
    # account exists or is disabled. The audit trail records the distinction.
    if user is None or not password_ok:
        account_lockout.record_failure(payload.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        logger.info("Login refused: inactive account %s", _mask_email(user.email))
        account_lockout.record_failure(payload.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Success: clear the bucket so transient typos don't carry forward.
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
    """Clear the session cookie and remove the corresponding server-side
    session row. Always returns 204, even with no session, so logout is
    idempotent."""
    from app.auth import COOKIE_NAME, parse_session_token
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed:
        user_id, _version, jti = parsed
        user = db.get(User, user_id)
        if user:
            _audit(db, user, "logout", f"{user.email} logged out")
        if jti:
            # Delete only this session; other open sessions for the same user remain.
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
    """Logged-in user updates their own password.

    Security-relevant side effects:
      - Bumps user.session_version, invalidating all other active sessions.
      - Deletes the user's existing session rows, then issues a fresh cookie
        and session row for the current request so the user isn't logged out
        by their own action.
      - Marks all outstanding password-reset tokens for this user as used.
    """
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    # Reject a no-op change to the same password: it would bump session_version
    # and boot every other device for no security gain.
    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(
            status_code=400,
            detail="New password must be different from the current password.",
        )

    # HIBP check on the new password before touching the DB.
    _reject_if_breached(payload.new_password)

    user.password_hash = hash_password(payload.new_password)
    user.session_version = (user.session_version or 0) + 1
    invalidated = invalidate_outstanding_reset_tokens(db, user.id)

    # The session_version bump already invalidates existing sessions; delete the
    # rows too so the admin session list doesn't fill with stale entries.
    db.execute(SessionRow.__table__.delete().where(SessionRow.user_id == user.id))

    # Issue a fresh session for the current device so the user isn't bounced to login.
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
    """Issue a password-reset email.

    Enumeration resistance is controlled by FORGOT_PASSWORD_ENUMERATION_SAFE
    (default True): the endpoint always returns 204 and never reveals whether
    the email maps to an account. Set it False for a friendlier UX that 404s on
    an unknown address, a lower-security trade-off. Either way a reset email is
    sent only to a real, active account and every attempt is audited.
    """
    settings = get_settings()
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not user.is_active:
        # In enumeration-safe mode, run (and discard) the same token work so
        # response timing doesn't reveal whether the account exists.
        if settings.FORGOT_PASSWORD_ENUMERATION_SAFE:
            generate_reset_token()
        _audit(db, None, "password_reset_no_account",
               f"Password reset attempted for unknown/inactive email: {_mask_email(payload.email)}")
        db.commit()
        if settings.FORGOT_PASSWORD_ENUMERATION_SAFE:
            return Response(status_code=204)
        # Enumeration-safe is off: reveal that the address is unknown.
        raise HTTPException(
            status_code=404,
            detail="We couldn't find an account with that email. Check the address or contact an administrator",
        )
    # Purge stale tokens here; there's no background job for this table.
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

    # Build the reset link; queue email send to background.
    base = get_settings().APP_BASE_URL.rstrip("/")
    reset_url = f"{base}/reset.html?token={raw_token}"
    background.add_task(
        notify_password_reset, user.email, user.name, reset_url,
    )
    return Response(status_code=204)


@router.post("/reset-password", status_code=204)
def reset_password(payload: ResetPasswordIn, db: Session = Depends(get_db)) -> Response:
    """Use a valid reset token to set a new password.

    Like change_password, this bumps the user's session_version so any
    currently-active sessions become invalid, and invalidates every other
    outstanding reset token for the same user.
    """
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

    # Check HIBP after validating the token so the breach signal isn't
    # returned to a caller with an invalid token.
    _reject_if_breached(payload.new_password)

    # Atomically consume the token via a guarded UPDATE that only succeeds if it
    # was still unused. If two requests race on the same token, exactly one
    # UPDATE affects a row; the loser sees rowcount 0 and is rejected, closing
    # the single-use replay window.
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
    # The used token is already consumed above; invalidate any remaining ones.
    invalidated = invalidate_outstanding_reset_tokens(db, user.id)

    # Delete session rows so the admin list doesn't fill with stale entries
    # (the session_version bump already makes them functionally invalid).
    db.execute(SessionRow.__table__.delete().where(SessionRow.user_id == user.id))

    _audit(db, user, "password_reset",
           f"{user.email} reset their password via token"
           + (f" (invalidated {invalidated} other outstanding reset link(s))"
              if invalidated else ""))
    db.commit()
    return Response(status_code=204)
