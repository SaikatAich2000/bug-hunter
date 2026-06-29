"""Sessions admin API (Keycloak-style).

Lets admins:
  - List every active server-side session row (with user, IP, UA, when
    it started, when it was last seen, when it expires).
  - Revoke an individual session. This boots that one device without
    affecting any other session for the same user.

All endpoints require admin. Managers cannot see or revoke sessions. This
is intentionally narrower than user and project management because the
ability to silently log other people out is a sensitive operation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import COOKIE_NAME, parse_session_token, require_admin
from app.database import get_db
from app.models import Activity, Session as SessionRow, User
from app.schemas import SessionOut

logger = logging.getLogger("bug_hunter.sessions")

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _audit(db: Session, actor: User, action: str, detail: str, entity_id: int | None = None) -> None:
    db.add(Activity(
        bug_id=None, entity_type="session", entity_id=entity_id,
        actor_user_id=actor.id, actor_name=actor.name,
        action=action, detail=detail,
    ))


def _is_current(request: Request, sess: SessionRow) -> bool:
    """Return True if this session row matches the cookie on the current request.
    Used to label the caller's own session and block self-revocation."""
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if not parsed:
        return False
    _uid, _ver, jti = parsed
    return jti is not None and jti == sess.jti


@router.get("", response_model=list[SessionOut])
def list_sessions(
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> list[dict]:
    """Return every non-expired session row, joined with the user's name
    and email for display. Sweeps expired rows on read so the admin's
    list stays accurate without a separate cron."""
    now = datetime.now(timezone.utc)

    # Sweep expired rows on every listing call so the admin view stays accurate
    # without a separate cron. A set-based DELETE is safe under concurrent access:
    # two admins racing this won't produce a StaleDataError the way
    # load-then-ORM-delete would when the other runner already removed the rows.
    # Errors are swallowed because the SELECT below filters expired rows anyway;
    # a failed sweep just means those rows linger until the next successful one.
    try:
        db.execute(delete(SessionRow).where(SessionRow.expires_at < now))
        db.commit()
    except SQLAlchemyError:
        logger.exception("Session expiry sweep failed; serving the listing anyway.")
        db.rollback()

    rows = db.scalars(
        select(SessionRow)
        .where(SessionRow.expires_at >= now)
        .order_by(SessionRow.last_seen_at.desc(), SessionRow.id.desc())
    ).all()

    # Batch-load users to avoid N+1 queries.
    user_ids = sorted({r.user_id for r in rows})
    user_map: dict[int, User] = {}
    if user_ids:
        for u in db.scalars(select(User).where(User.id.in_(user_ids))).all():
            user_map[u.id] = u

    out: list[dict] = []
    for r in rows:
        u = user_map.get(r.user_id)
        out.append({
            "id": r.id,
            "user_id": r.user_id,
            "user_name": u.name if u else None,
            "user_email": u.email if u else None,
            "user_role": u.role if u else None,
            "ip_address": r.ip_address or "",
            "user_agent": r.user_agent or "",
            "created_at": r.created_at,
            "last_seen_at": r.last_seen_at,
            "expires_at": r.expires_at,
            "is_current": _is_current(request, r),
        })
    return out


@router.delete("/{session_id}", status_code=200)
def revoke_session(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> dict[str, str]:
    """Revoke (delete) a single session row. The next request from that
    cookie will fail validation and the user will be bounced to login.
    Other sessions for the same user are not touched."""
    sess = db.get(SessionRow, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Prevent admins from revoking their own current session by mistake; there
    # is no undo. Use /api/auth/logout to end your own session cleanly.
    if _is_current(request, sess):
        raise HTTPException(
            status_code=400,
            detail="You can't revoke your own current session — use Log out instead.",
        )

    target = db.get(User, sess.user_id)
    target_label = (
        f"{target.name} <{target.email}>" if target else f"user #{sess.user_id}"
    )
    db.delete(sess)
    _audit(
        db, actor, "session_revoked", f"Revoked session for {target_label}",
        entity_id=sess.user_id,
    )
    db.commit()
    return {"message": "Session revoked"}
