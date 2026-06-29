"""Per-user in-app notifications.

The in-app counterpart to app/email_service.py. Where a route sends an email
about an event, it also writes one notification row per recipient via
``notify()`` to the same recipients the email layer targets (reporter +
assignees minus the actor, event managers, etc.). Notifications are therefore
per-user and role-respecting: a row is only ever created for a user already
entitled to know about the event, and no endpoint exposes another user's
notifications.

Unlike the email layer (which runs in a BackgroundTask off primitive
snapshots), notifications are small DB inserts written synchronously on the
request's own Session and committed with it. That keeps them transactional
with the change that caused them and immediately assertable in tests.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Notification, _utcnow

if TYPE_CHECKING:  # avoid importing FastAPI just for the type hint
    from fastapi import BackgroundTasks


def notify(
    db: Session,
    user_ids: Iterable[int | None],
    *,
    kind: str,
    title: str,
    body: str = "",
    bug_id: int | None = None,
    event_id: int | None = None,
    actor_name: str = "",
    exclude: int | None = None,
    background: "BackgroundTasks | None" = None,
) -> list[int]:
    """Queue an in-app notification for each distinct recipient, and (when a
    ``background`` task runner is given) schedule an immediate web push to the
    same recipients.

    Rows are ``db.add``-ed but not committed; the caller commits as part of the
    same transaction as the triggering change. ``None`` ids, the ``exclude`` id
    (the actor), and duplicates are skipped. Returns the list of recipient user
    ids actually queued.

    The web push (if scheduled) fires regardless of the email-digest setting:
    push is the real-time channel, the digest only batches email.
    """
    # In immediate-email mode the row is born already-emailed, so switching to
    # digest mode later never re-sends past notifications. In digest mode it
    # stays NULL so the daily job picks each row up exactly once.
    # (read_at tracks the in-app read state separately.)
    emailed_at = None if get_settings().EMAIL_DIGEST_ENABLED else _utcnow()

    seen: set[int] = set()
    recipients: list[int] = []
    for uid in user_ids:
        if uid is None or uid == exclude or uid in seen:
            continue
        seen.add(uid)
        recipients.append(uid)
        db.add(Notification(
            user_id=uid,
            kind=kind,
            title=title,
            body=body,
            bug_id=bug_id,
            event_id=event_id,
            actor_name=actor_name,
            emailed_at=emailed_at,
        ))

    if background is not None and recipients:
        # Lazy import: keeps this module free of push/FCM dependencies for
        # callers that never schedule a push.
        from app import push_service
        push_service.schedule(
            background, recipients, title=title, body=body,
            bug_id=bug_id, event_id=event_id,
        )
    return recipients
