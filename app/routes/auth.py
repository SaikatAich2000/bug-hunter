"""Authentication endpoints — login, logout, password management."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import (
    PASSWORD_RESET_TTL,
    clear_session_cookie,
    generate_reset_token,
    get_current_user,
    hash_password,
    hash_reset_token,
    invalidate_outstanding_reset_tokens,
    new_jti,
    set_session_cookie,
    verify_password,
)
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

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _audit(db: Session, actor: User | None, action: str, detail: str, entity_id: int | None = None) -> None:
    db.add(Activity(
        bug_id=None, entity_type="auth", entity_id=entity_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action, detail=detail,
    ))


def _client_ip(request: Request) -> str:
    """Best-effort client IP for the session log. Honors X-Forwarded-For
    when running behind a reverse proxy, falls back to the socket addr.
    Capped at 64 chars to fit the column."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        # X-F-F is a comma-separated chain; the leftmost is the original client.
        ip = fwd.split(",")[0].strip()
    elif request.client and request.client.host:
        ip = request.client.host
    else:
        ip = ""
    return ip[:64]


@router.post("/login", response_model=MeOut)
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> User:
    """Verify credentials, create a server-side session row, and set the
    signed cookie. The cookie carries a `jti` that maps back to the row,
    which is what makes per-session admin revocation possible."""
    # LoginIn already lowercases the email — no need to .lower() again here.
    user = db.scalar(select(User).where(User.email == payload.email))
    # Unified error message for all failure modes — never leak whether the
    # email exists OR whether an existing account is disabled. Previously
    # we returned 401 for bad creds but 403 for disabled accounts, which
    # let an attacker who knew a valid password distinguish "this account
    # exists but is disabled" from "wrong password". Both now return the
    # same 401. Audit log still records the distinction server-side.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        logger.info("Login refused: inactive account %s", user.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")

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
    session row. Always 204 even if there's no session — logout should be
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
            # Single-row delete keyed by the token's jti. Other sessions
            # for the same user are untouched.
            db.execute(
                SessionRow.__table__.delete().where(SessionRow.jti == jti)
            )
        db.commit()
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)) -> User:
    """Return the currently logged-in user. Used by the frontend on every load."""
    return user


@router.post("/change-password", status_code=204)
def change_password(
    payload: ChangePasswordIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Logged-in user updates their own password.

    Side effects (security-relevant):
      - Bumps user.session_version → all OTHER active sessions invalidated.
      - Deletes ALL the user's existing session rows (Keycloak-style: a
        password change should boot every device, including this one in
        terms of token validity, then re-establish a fresh session for
        the current request).
      - Issues a fresh cookie + session row for the current request so
        the user isn't immediately logged out by their own action.
      - Marks all outstanding password-reset tokens for this user as used.
    """
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    user.password_hash = hash_password(payload.new_password)
    user.session_version = (user.session_version or 0) + 1
    invalidated = invalidate_outstanding_reset_tokens(db, user.id)

    # Wipe all existing session rows for this user; they're invalidated by
    # the session_version bump above anyway, but keeping rows around would
    # clutter the admin "active sessions" view.
    db.execute(SessionRow.__table__.delete().where(SessionRow.user_id == user.id))

    # Re-establish a fresh session for the device that just changed the
    # password, so they're not bounced to login immediately.
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
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Issue a password-reset email. Always 204 — never reveal whether the email exists."""
    user = db.scalar(select(User).where(User.email == payload.email))
    # IMPORTANT: respond identically whether or not the user exists. This
    # prevents an attacker from probing the system to enumerate accounts.
    if user is not None and user.is_active:
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
    currently-active sessions become invalid (the attacker who guessed
    your password loses their session the moment you reset). It also
    invalidates every other outstanding reset token for the same user.
    """
    h = hash_reset_token(payload.token)
    prt = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == h))
    if prt is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    now = datetime.now(timezone.utc)
    expires = prt.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if prt.used_at is not None or expires < now:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user = db.get(User, prt.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user.password_hash = hash_password(payload.new_password)
    user.session_version = (user.session_version or 0) + 1
    prt.used_at = now
    invalidated = invalidate_outstanding_reset_tokens(db, user.id)

    # Reset == "forget every device that was logged in as me". The
    # session_version bump alone would do this implicitly, but we also
    # delete the session rows so the admin session-list isn't littered
    # with stale entries for accounts that just got reset.
    db.execute(SessionRow.__table__.delete().where(SessionRow.user_id == user.id))

    _audit(db, user, "password_reset",
           f"{user.email} reset their password via token"
           + (f" (invalidated {invalidated - 1} other outstanding reset link(s))"
              if invalidated > 1 else ""))
    db.commit()
    return Response(status_code=204)
