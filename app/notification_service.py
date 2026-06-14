"""Per-user in-app notifications (v3.0).

The in-app counterpart to app/email_service.py. Where a route sends an email
about an event, it ALSO writes one notification row per recipient via
``notify()`` — to the SAME recipients the email layer targets (reporter +
assignees minus the actor, event managers, etc.). Notifications are therefore
inherently per-user and role-respecting: a row is only ever created for a user
already entitled to know about the event, and no endpoint exposes another
user's notifications.

Unlike the email layer (which runs in a BackgroundTask off primitive
snapshots), notifications are tiny DB inserts, so they are written
synchronously on the request's own Session and committed with it. That keeps
them transactional with the change that caused them and makes them
immediately assertable in tests.
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
    ``background`` task runner is given) schedule an IMMEDIATE web push to the
    same recipients.

    Rows are ``db.add``-ed but NOT committed — the caller commits as part of
    the same transaction as the triggering change. ``None`` ids, the ``exclude``
    id (the actor — you don't notify yourself), and duplicates are all skipped.
    Returns the list of recipient user ids actually queued.

    The web push (if scheduled) fires right away regardless of the email-digest
    setting — push is the real-time channel; the digest only batches *email*.
    """
    # Email-obligation marker. In immediate-email mode every operation's email
    # goes out right now (from the route's BackgroundTask), so the row is born
    # already-emailed — this is what guarantees that turning the daily digest ON
    # later never re-sends an old operation. In digest mode we leave it NULL so
    # the daily job picks the operation up exactly once. (read_at — the in-app
    # read state — is independent of this.)
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
        # Lazy import keeps notification_service free of the push/FCM deps for
        # callers that never push.
        from app import push_service
        push_service.schedule(
            background, recipients, title=title, body=body,
            bug_id=bug_id, event_id=event_id,
        )
    return recipients
