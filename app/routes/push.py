"""Web push subscription API (Firebase Cloud Messaging).

Every mutating endpoint is scoped to the authenticated user — you register or
remove only YOUR OWN device tokens. ``GET /config`` exposes the *public*
Firebase web config so the browser can initialise messaging; the secret
service-account JSON never leaves the server.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import push_service
from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import User
from app.push_service import PushTokenConflict
from app.schemas import PushConfigOut, PushSubscribeIn, PushUnsubscribeIn

router = APIRouter(prefix="/api/push", tags=["push"])


@router.get("/config", response_model=PushConfigOut)
def push_config() -> PushConfigOut:
    """Public Firebase web config for the browser. ``enabled`` is False when web
    push isn't fully configured, so the frontend can hide its toggle."""
    s = get_settings()
    configured = bool(
        s.WEB_PUSH_ENABLED and s.FIREBASE_API_KEY and s.FIREBASE_VAPID_KEY
    )
    if not configured:
        return PushConfigOut(enabled=False)
    return PushConfigOut(
        enabled=True,
        api_key=s.FIREBASE_API_KEY,
        auth_domain=s.FIREBASE_AUTH_DOMAIN,
        project_id=s.FIREBASE_PROJECT_ID,
        messaging_sender_id=s.FIREBASE_MESSAGING_SENDER_ID,
        app_id=s.FIREBASE_APP_ID,
        vapid_key=s.FIREBASE_VAPID_KEY,
    )


@router.post("/subscribe")
def subscribe(
    payload: PushSubscribeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    """Register (or refresh) this browser/device's FCM token for the user."""
    try:
        push_service.register(
            db,
            user_id=user.id,
            token=payload.token,
            platform=payload.platform,
            user_agent=payload.user_agent,
        )
    except PushTokenConflict as exc:
        # Token already bound to a different account — refuse rather than let a
        # replayed token hijack that device's push.
        raise HTTPException(
            status_code=409,
            detail="This device token is already registered to another account.",
        ) from exc
    db.commit()
    return {"ok": True}


@router.post("/unsubscribe")
def unsubscribe(
    payload: PushUnsubscribeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    """Remove this device's token (called when the user turns push off)."""
    removed = push_service.remove(db, token=payload.token, user_id=user.id)
    db.commit()
    return {"ok": removed}
