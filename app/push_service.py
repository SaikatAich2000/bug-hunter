"""Web push orchestration.

Registers/removes a user's device tokens and fans a single notification out to
all of a user's devices via FCM — sent **immediately** when an operation
happens (wired into the same trigger points as ``notification_service.notify``),
never batched into the email digest.

The actual FCM network call lives in ``app.fcm_transport`` (mocked in tests);
this module is the testable orchestration: token storage, recipient selection,
and pruning of tokens FCM reports as dead.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import fcm_transport
from app.config import get_settings
from app.database import SessionLocal
from app.models import PushSubscription, _utcnow

if TYPE_CHECKING:
    from fastapi import BackgroundTasks

logger = logging.getLogger("bug_hunter.push")


def _deep_link(bug_id: int | None, event_id: int | None) -> str:
    """The in-app URL a push should open (matches the SPA's hash routes)."""
    if bug_id:
        return f"/#bug={bug_id}"
    if event_id:
        return f"/#event={event_id}"
    return "/"


def schedule(
    background: "BackgroundTasks",
    user_ids: Iterable[int | None],
    *,
    title: str,
    body: str,
    bug_id: int | None = None,
    event_id: int | None = None,
) -> None:
    """Schedule an immediate push to ``user_ids`` as a background task.

    A no-op (nothing scheduled) when web push is disabled, so callers never pay
    for it when the feature is off.
    """
    if not get_settings().WEB_PUSH_ENABLED:
        return
    ids = [uid for uid in user_ids if uid is not None]
    if not ids:
        return
    background.add_task(
        push_to_users, ids, title=title, body=body,
        url=_deep_link(bug_id, event_id),
    )


def register(
    db: Session,
    *,
    user_id: int,
    token: str,
    platform: str = "web",
    user_agent: str = "",
) -> PushSubscription:
    """Upsert a device's FCM token for ``user_id``.

    Tokens are globally unique per device install, so if one already exists we
    re-home it to this user (it may have moved browsers/accounts) and refresh
    ``last_seen_at`` rather than creating a duplicate.
    """
    def _rehome(sub: PushSubscription) -> PushSubscription:
        sub.user_id = user_id
        sub.platform = platform or "web"
        sub.user_agent = (user_agent or "")[:400]
        sub.last_seen_at = _utcnow()
        return sub

    existing = db.scalar(
        select(PushSubscription).where(PushSubscription.token == token)
    )
    if existing is not None:
        return _rehome(existing)
    sub = PushSubscription(
        user_id=user_id,
        token=token,
        platform=platform or "web",
        user_agent=(user_agent or "")[:400],
    )
    db.add(sub)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent subscribe of the same globally-unique token won the
        # insert race — re-home the row that landed instead of 500ing.
        db.rollback()
        other = db.scalar(
            select(PushSubscription).where(PushSubscription.token == token)
        )
        if other is None:
            raise
        return _rehome(other)
    return sub


def remove(db: Session, *, token: str, user_id: int | None = None) -> bool:
    """Delete a subscription by token (optionally constrained to a user).
    Returns True if a row was removed."""
    query = select(PushSubscription).where(PushSubscription.token == token)
    if user_id is not None:
        query = query.where(PushSubscription.user_id == user_id)
    sub = db.scalar(query)
    if sub is None:
        return False
    db.delete(sub)
    return True


def _subs_for_users(db: Session, user_ids: Iterable[int]) -> list[PushSubscription]:
    ids = {uid for uid in user_ids if uid is not None}
    if not ids:
        return []
    return list(db.scalars(
        select(PushSubscription).where(PushSubscription.user_id.in_(ids))
    ).all())


def push_to_users(
    user_ids: Iterable[int | None],
    *,
    title: str,
    body: str,
    url: str = "",
) -> int:
    """Immediately push to every device of the given users; returns the number
    of tokens delivered to.

    Designed to run in a FastAPI ``BackgroundTask`` after the request session
    closed, so it opens its OWN session. A no-op (returns 0) unless web push is
    enabled. Tokens FCM reports as dead are pruned.
    """
    if not get_settings().WEB_PUSH_ENABLED:
        return 0
    ids = [uid for uid in user_ids if uid is not None]
    if not ids:
        return 0

    db = SessionLocal()
    try:
        subs = _subs_for_users(db, ids)
        if not subs:
            return 0
        tokens = [s.token for s in subs]
        dead = set(fcm_transport.send(tokens, title=title, body=body, url=url))
        if dead:
            for sub in subs:
                if sub.token in dead:
                    db.delete(sub)
            db.commit()
        return len(tokens) - len(dead)
    except Exception:  # noqa: BLE001 — push must never break the request flow
        logger.exception("push_to_users failed")
        db.rollback()
        return 0
    finally:
        db.close()
