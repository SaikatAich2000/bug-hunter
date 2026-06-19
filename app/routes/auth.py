"""Authentication endpoints — login, logout, password management."""
from __future__ import annotations

import ipaddress
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
    set_session_cookie,
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

# Precomputed dummy bcrypt hash used by the login path when no user matches the
# supplied email. Without it, the no-user branch skips the bcrypt verify and
# returns ~50ms faster than the wrong-password path — an attacker could
# enumerate accounts by timing the login response. Verifying against this dummy
# in the no-user branch equalises the work, closing the timing oracle. It's a
# bcrypt hash of a random string the server never accepts; even if leaked,
# nobody could log in with it.
_DUMMY_PASSWORD_HASH = hash_password("dummy-not-a-real-credential")

router = APIRouter(prefix="/api/auth", tags=["auth"])



# Extracted to a module constant to avoid duplicating the literal.
_DETAIL_INVALID_RESET_TOKEN = "Invalid or expired reset token"

def _audit(db: Session, actor: User | None, action: str, detail: str, entity_id: int | None = None) -> None:
    db.add(Activity(
        bug_id=None, entity_type="auth", entity_id=entity_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action, detail=detail,
    ))


def _reject_if_breached(plain: str) -> None:
    """Refuse to accept a password that appears in the HIBP corpus.

    Called from every code path that sets a password (the login flow is
    excluded — credentials can't be changed at login time). Fail-open on
    network errors so an HIBP outage doesn't block legitimate password
    changes; see app/password_breach.py.
    """
    if is_password_breached(plain):
        raise HTTPException(
            status_code=400,
            detail="This password appears in a known breach corpus. "
                   "Please choose a different one.",
        )


def _mask_email(email: str) -> str:
    """Mask the local part of an email for safe inclusion in logs.

    Log lines feed centralised log stores (Loki / CloudWatch / etc.) whose
    access controls are usually broader than the app DB's. Writing raw emails
    there is unnecessary PII leakage when a one-character-plus-asterisks form
    keeps the line just as useful for diagnosing the event.
    ``alice@example.com`` -> ``a***@example.com``.
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

    Only honours X-Forwarded-For when the deploy explicitly opted in via
    TRUST_PROXY_FORWARDED_FOR. Without this gate, a client behind a non-proxied
    deploy can set X-Forwarded-For to anything and spoof the IP recorded in
    their session row and audit entries — making it look like the login came
    from a different machine. Matches the rate-limit middleware's ``_client_ip``
    in app/main.py.
    """
    if get_settings().TRUST_PROXY_FORWARDED_FOR:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            # X-F-F is a comma-separated chain; the leftmost is the original client.
            ip = fwd.split(",")[0].strip()
            # Only trust a value that parses as an IP — a crafted XFF could
            # otherwise persist arbitrary text (control chars, misleading
            # "spoofed" labels) into the session list / audit trail.
            try:
                ipaddress.ip_address(ip)
                return ip[:64]
            except ValueError:
                pass
    if request.client and request.client.host:
        return request.client.host[:64]
    return ""


@router.post("/login", response_model=MeOut)
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> User:
    """Verify credentials, create a server-side session row, and set the
    signed cookie. The cookie carries a `jti` that maps back to the row,
    which is what makes per-session admin revocation possible."""
    # Short-circuit if this email is currently locked out. Raised before the
    # bcrypt verify so a flood of bad logins doesn't amplify into a flood of
    # bcrypt rounds, keeping the lockout cheap to enforce.
    account_lockout.check_locked(payload.email)

    # LoginIn already lowercases the email — no need to .lower() again here.
    user = db.scalar(select(User).where(User.email == payload.email))
    # Equalise the timing of unknown-email vs wrong-password. Skipping
    # verify_password when user is None would let an attacker enumerate
    # accounts by measuring response latency (bcrypt costs ~50 ms; the no-user
    # branch returns in <1 ms). The bcrypt verify always runs — against the
    # real hash when there's a user, against a server-side dummy otherwise.
    # password_ok stays False for the no-user case because the dummy hash never
    # matches the supplied password.
    if user is None:
        verify_password(payload.password, _DUMMY_PASSWORD_HASH)
        password_ok = False
    else:
        password_ok = verify_password(payload.password, user.password_hash)
    # Unified error message for all failure modes — never leak whether the
    # email exists or whether an existing account is disabled. Returning a
    # distinct status for a disabled account would let an attacker who knew a
    # valid password distinguish "exists but disabled" from "wrong password";
    # both return the same 401. The audit log still records the distinction
    # server-side.
    if user is None or not password_ok:
        # Tick the lockout counter for every failed attempt, including ones
        # against unknown emails. Ticking only known emails would let an
        # attacker enumerate accounts by which addresses ever lock.
        account_lockout.record_failure(payload.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        # Keep the raw email out of INFO logs; the masked form stays
        # diagnosable without writing PII to centralised log stores.
        logger.info("Login refused: inactive account %s", _mask_email(user.email))
        # An inactive account is a failed login, so still tick the counter.
        account_lockout.record_failure(payload.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Success — clear the bucket so transient typos don't carry forward.
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

    # HIBP check on the new password — fail before touching the DB.
    _reject_if_breached(payload.new_password)

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
    db: Session = Depends(get_db),
) -> Response:
    """Issue a password-reset email.

    Enumeration resistance is controlled by FORGOT_PASSWORD_ENUMERATION_SAFE
    (default True = most secure): the endpoint then always returns 204 and
    never reveals whether the email maps to an account — the canonical "if an
    account exists, a link has been sent" behaviour. Set the flag False for the
    friendlier UX that 404s on an unknown address, a documented lower-security
    trade-off. Either way a reset email is sent only to a real, active account
    and every attempt is audited.
    """
    settings = get_settings()
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not user.is_active:
        # Enumeration-resistant mode: do the same token-derivation work the real
        # path does (and discard it) so response timing doesn't betray whether
        # the account exists — the same idea as login's dummy bcrypt verify.
        if settings.FORGOT_PASSWORD_ENUMERATION_SAFE:
            generate_reset_token()
        # Always record the server-side signal for auditing, regardless of mode.
        _audit(db, None, "password_reset_no_account",
               f"Password reset attempted for unknown/inactive email: {_mask_email(payload.email)}")
        db.commit()
        if settings.FORGOT_PASSWORD_ENUMERATION_SAFE:
            # Enumeration-resistant default: identical 204 to the success path,
            # so an unauthenticated caller can't probe which emails exist.
            return Response(status_code=204)
        # Opt-out: reveal that the address is unknown for a friendlier UX.
        raise HTTPException(
            status_code=404,
            detail="We couldn't find an account with that email. Check the address or contact an administrator",
        )
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
        raise HTTPException(status_code=400, detail=_DETAIL_INVALID_RESET_TOKEN)
    now = datetime.now(timezone.utc)
    expires = prt.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if prt.used_at is not None or expires < now:
        raise HTTPException(status_code=400, detail=_DETAIL_INVALID_RESET_TOKEN)

    user = db.get(User, prt.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail=_DETAIL_INVALID_RESET_TOKEN)

    # HIBP check before accepting the reset, done after the token is validated
    # so the breach signal isn't leaked back to a holder of an invalid token.
    _reject_if_breached(payload.new_password)

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
