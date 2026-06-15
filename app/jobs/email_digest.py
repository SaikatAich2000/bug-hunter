"""Daily email-digest job.

Run once a day (e.g. from host cron) with::

    python -m app.jobs.email_digest

When ``EMAIL_DIGEST_ENABLED`` is on, the per-operation work-item emails (new
item / update / assignment / comment / event) are NOT sent immediately — each
operation is recorded as a notification row instead (the same rows that power
the in-app bell, written by ``app.notification_service.notify``). This job
sweeps up every user's un-emailed operations from the last
``EMAIL_DIGEST_LOOKBACK_HOURS`` and sends ONE grouped email per user, then
stamps ``notifications.emailed_at`` so the same operation is never re-sent.

Design notes
------------
* **Idempotent.** Rows are selected on ``emailed_at IS NULL`` and stamped after
  a successful send, so re-running the job (or running it twice a day) never
  double-sends.
* **Bounded.** The ``created_at >= now - lookback`` window means the very first
  run after deploying this feature can't replay the entire notification history
  — only the last ~day is ever considered. Existing rows (``emailed_at`` NULL
  by virtue of the new column) age out of the window instead of flooding inboxes.
* **Per-user & role-respecting for free.** Notification rows only exist for
  users already entitled to know about the operation, so the digest inherits
  that scoping with no extra checks.
* **Security emails are untouched.** Password-reset and other transactional
  emails never become notification rows and always send immediately — this job
  only ever deals with the five work-item *operation* categories.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import email_service
from app.config import get_settings
from app.database import SessionLocal
from app.models import Notification, User, _utcnow

logger = logging.getLogger("bug_hunter.digest")

_SUBJECT_PREFIX = "[Bug Hunter]"

# Display order + human label for each notification ``kind``. Mirrors the five
# operation categories the in-app bell already uses.
_CATEGORY_ORDER: tuple[tuple[str, str], ...] = (
    ("assigned", "🎯 Assigned to you"),
    ("reported", "🆕 Reported by / with you"),
    ("updated", "✏️ Updates"),
    ("comment", "💬 Comments"),
    ("event", "📅 Events"),
)
_KNOWN_KINDS = frozenset(kind for kind, _ in _CATEGORY_ORDER)


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def _link(base: str, row: Notification) -> str:
    """Deep-link to the work item or event the operation refers to."""
    if row.bug_id:
        return f"{base}/#bug={row.bug_id}"
    if row.event_id:
        return f"{base}/#event={row.event_id}"
    return base


def _section_lines(label: str, rows: list[Notification], base: str) -> list[str]:
    """Render one category section: a header + one bullet per operation."""
    lines = [f"{label} ({len(rows)})"]
    for row in rows:
        lines.append(f"  • {row.title}")
        if row.body:
            lines.append(f"    {row.body}")
        lines.append(f"    {_link(base, row)}")
    lines.append("")
    return lines


def render_digest(user: User, rows: list[Notification]) -> tuple[str, str]:
    """Build the (subject, body) for one user's batched operations."""
    base = get_settings().APP_BASE_URL.rstrip("/")
    by_kind: dict[str, list[Notification]] = {}
    for row in rows:
        by_kind.setdefault(row.kind, []).append(row)

    total = len(rows)
    body_lines = [
        f"Hi {user.name or 'there'},",
        "",
        f"Here's your Bug Hunter activity digest — {total} update{_plural(total)} "
        "since the last one.",
        "",
    ]
    for kind, label in _CATEGORY_ORDER:
        section = by_kind.get(kind)
        if section:
            body_lines += _section_lines(label, section, base)

    # Future-proofing: surface any kind we don't have a category for rather
    # than silently dropping it.
    other = [r for r in rows if r.kind not in _KNOWN_KINDS]
    if other:
        body_lines += _section_lines("📌 Other", other, base)

    body_lines.append("— Bug Hunter")
    subject = f"{_SUBJECT_PREFIX} Your activity digest — {total} update{_plural(total)}"
    return subject, "\n".join(body_lines)


def _group_by_user(rows: list[Notification]) -> dict[int, list[Notification]]:
    grouped: dict[int, list[Notification]] = {}
    for row in rows:
        grouped.setdefault(row.user_id, []).append(row)
    return grouped


def run_digest(
    db: Session,
    *,
    now: datetime | None = None,
    lookback_hours: int | None = None,
) -> dict[str, int]:
    """Send one digest email per user for their un-emailed operations.

    Selects ``emailed_at IS NULL`` rows created within the lookback window,
    groups them by user, emails each deliverable user once, and stamps the
    sent rows. Returns ``{"users", "emails_sent", "operations"}`` for logging
    and tests. Commits once at the end.
    """
    settings = get_settings()
    now = now or _utcnow()
    if lookback_hours is None:
        lookback_hours = settings.EMAIL_DIGEST_LOOKBACK_HOURS
    cutoff = now - timedelta(hours=lookback_hours)

    rows = list(db.scalars(
        select(Notification)
        .where(Notification.emailed_at.is_(None), Notification.created_at >= cutoff)
        .order_by(Notification.user_id, Notification.created_at)
    ).all())
    if not rows:
        return {"users": 0, "emails_sent": 0, "operations": 0}

    by_user = _group_by_user(rows)
    emails_sent = 0
    operations = 0
    for user_id, user_rows in by_user.items():
        user = db.get(User, user_id)
        if user is None or not user.is_active or not user.email:
            # Nothing to deliver to — leave the rows un-stamped; the lookback
            # window ages them out on its own (they remain visible in-app).
            continue
        subject, body = render_digest(user, user_rows)
        email_service.deliver(subject, [user.email], body)
        for row in user_rows:
            row.emailed_at = now
        emails_sent += 1
        operations += len(user_rows)

    db.commit()
    return {
        "users": len(by_user),
        "emails_sent": emails_sent,
        "operations": operations,
    }


def main() -> int:
    """Entry point for ``python -m app.jobs.email_digest``."""
    settings = get_settings()
    if not settings.EMAIL_DIGEST_ENABLED:
        logger.info(
            "EMAIL_DIGEST_ENABLED is off — digest job is a no-op "
            "(work-item emails are sending immediately)."
        )
        return 0
    db = SessionLocal()
    try:
        stats = run_digest(db)
        logger.info(
            "Email digest complete: %s email(s) sent covering %s operation(s) "
            "across %s user(s).",
            stats["emails_sent"], stats["operations"], stats["users"],
        )
        return 0
    except Exception:
        # Broad on purpose: any failure becomes a non-zero exit code (logged
        # below) so a cron wrapper can detect it, rather than a raw traceback.
        logger.exception("Email digest job failed.")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    raise SystemExit(main())
