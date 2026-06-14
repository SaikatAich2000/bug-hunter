"""Email service.

Three backends:
  - console   : just logs the email to stdout. Default; perfect for dev.
  - smtp      : sends via real SMTP server.
  - disabled  : no-op. For tests.

All public functions are designed to be called from a FastAPI
BackgroundTasks instance: they take pre-fetched primitives (not DB
sessions or ORM objects) so they don't blow up if called after the
request has finished and the session is closed.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Iterable

from app.config import Settings, get_settings

logger = logging.getLogger("bug_hunter.email")

# Repeated section label used by every email body that has a free-text
# description block. Extracted so Sonar's S1192 duplicate-string-literal
# rule stays quiet.
_DESC_LABEL = "Description:"


# ---------------------------------------------------------------------------
# Snapshot dataclasses (no SQLAlchemy objects past this point)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class UserSnapshot:
    id: int
    name: str
    email: str

    @property
    def display(self) -> str:
        return f"{self.name} <{self.email}>"


@dataclass(frozen=True)
class BugSnapshot:
    """Snapshot of a single work-item row passed to the email layer.

    `item_type` is the work-item flavor — "Bug" / "Requirement" / "Task".
    `event_name` is set when the item belongs to an event; rendered as a
    standalone metadata line so the recipient can see at a glance that
    this is part of, say, today's standup. Existing rows created before
    these columns existed default to "Bug" / None, so older callers that
    don't set the fields still produce correct emails.
    """
    id: int
    title: str
    project_name: str
    status: str
    priority: str
    environment: str
    description: str
    reporter: UserSnapshot | None
    assignees: tuple[UserSnapshot, ...]
    item_type: str = "Bug"
    event_name: str | None = None


# ---------------------------------------------------------------------------
# Low-level transport
# ---------------------------------------------------------------------------
def _send_smtp(settings: Settings, msg: EmailMessage) -> None:
    """Synchronous SMTP send. Called from a worker thread by FastAPI."""
    if not settings.SMTP_HOST:
        logger.warning("SMTP backend selected but SMTP_HOST is empty; dropping email.")
        return

    try:
        # Build an SSL context with EXPLICIT hostname + cert verification
        # and an EXPLICIT minimum TLS version. ssl.create_default_context()
        # already sets sane defaults on Python 3.10+, but Sonar's
        # python:S4830 + python:S4423 rules want the posture stated locally
        # so it's auditable without consulting stdlib internals.
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        if settings.SMTP_USE_SSL:
            with smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT,
                timeout=settings.SMTP_TIMEOUT, context=ctx,
            ) as s:
                if settings.SMTP_USERNAME:
                    s.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(
                settings.SMTP_HOST, settings.SMTP_PORT,
                timeout=settings.SMTP_TIMEOUT,
            ) as s:
                s.ehlo()
                if settings.SMTP_USE_TLS:
                    s.starttls(context=ctx)
                    s.ehlo()
                if settings.SMTP_USERNAME:
                    s.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                s.send_message(msg)
        logger.info("SMTP email sent: subject=%r to=%s", msg["Subject"], msg["To"])
    except (smtplib.SMTPException, OSError):
        # Never let mailer failures break the API. We narrow to network and
        # SMTP-protocol errors so programmer mistakes (e.g. bad header type)
        # still surface in tests instead of getting swallowed.
        logger.exception("Failed to send email via SMTP")


def _send_console(msg: EmailMessage) -> None:
    body = msg.get_content() if msg.is_multipart() is False else "(multipart)"
    logger.info(
        "[console-email]\n  From: %s\n  To: %s\n  Subject: %s\n  ----\n%s\n  ----",
        msg["From"], msg["To"], msg["Subject"], body.strip(),
    )


def _build(subject: str, to: list[str], body: str, settings: Settings) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def deliver(subject: str, to: list[str], body: str) -> None:
    """Public dispatch — pick the right backend based on settings."""
    settings = get_settings()
    if settings.EMAIL_BACKEND == "disabled":
        return
    to = sorted({addr.strip() for addr in to if addr and addr.strip()})
    if not to:
        return

    msg = _build(subject, to, body, settings)
    if settings.EMAIL_BACKEND == "smtp":
        _send_smtp(settings, msg)
    else:
        _send_console(msg)


def _digest_owns_work_item_email() -> bool:
    """True when the once-a-day digest job — not an immediate send — owns the
    work-item *operation* emails (new item / update / assignment / comment /
    event). When EMAIL_DIGEST_ENABLED is on, those `notify_*` functions become
    no-ops and the operation is delivered later by `app.jobs.email_digest`,
    batched into one email per user.

    Security/transactional emails (e.g. password reset) NEVER consult this and
    always send immediately — they are not operations and must not wait a day.
    """
    return get_settings().EMAIL_DIGEST_ENABLED


# ---------------------------------------------------------------------------
# Recipient selection
# ---------------------------------------------------------------------------
def _recipients(bug: BugSnapshot, exclude_user_id: int | None) -> list[str]:
    """Reporter + all assignees, deduped, optionally minus the actor."""
    seen: dict[str, None] = {}
    candidates: list[UserSnapshot] = []
    if bug.reporter:
        candidates.append(bug.reporter)
    candidates.extend(bug.assignees)
    out: list[str] = []
    for u in candidates:
        if exclude_user_id is not None and u.id == exclude_user_id:
            continue
        if not u.email:
            continue
        key = u.email.lower()
        if key in seen:
            continue
        seen[key] = None
        out.append(u.email)
    return out


# ---------------------------------------------------------------------------
# Notification helpers — these are what routes call.
# Each takes only primitive data (no DB session, no ORM objects).
# ---------------------------------------------------------------------------
def _bug_link(bug_id: int) -> str:
    base = get_settings().APP_BASE_URL.rstrip("/")
    return f"{base}/#bug={bug_id}"


def _item_label(bug: BugSnapshot) -> str:
    """The grammatical noun for the item — 'bug' / 'requirement' / 'task'."""
    return (bug.item_type or "Bug").lower()


def _bug_meta_lines(bug: BugSnapshot) -> list[str]:
    """Plain-text metadata block reused across every notification email."""
    label_cap = (bug.item_type or "Bug").capitalize()
    lines = [
        f"{label_cap} #{bug.id}: {bug.title}",
        f"Type:        {bug.item_type or 'Bug'}",
    ]
    if bug.event_name:
        lines.append(f"Event:       {bug.event_name}")
    lines += [
        f"Project:     {bug.project_name}",
        f"Status:      {bug.status}",
        f"Priority:    {bug.priority}",
        f"Environment: {bug.environment}",
        f"Reporter:    {bug.reporter.display if bug.reporter else '—'}",
        "Assignees:   " + (
            ", ".join(a.display for a in bug.assignees) if bug.assignees else "—"
        ),
    ]
    return lines


def notify_bug_created(bug: BugSnapshot, actor_user_id: int | None) -> None:
    if _digest_owns_work_item_email():
        return
    to = _recipients(bug, exclude_user_id=actor_user_id)
    if not to:
        return
    label = _item_label(bug)
    subject = f"[Bug Hunter] New {label} #{bug.id}: {bug.title}"
    lines = [f"A new {label} has been created.", ""]
    lines += _bug_meta_lines(bug)
    if bug.description:
        lines += ["", _DESC_LABEL, bug.description]
    lines += ["", f"View: {_bug_link(bug.id)}"]
    deliver(subject, to, "\n".join(lines))


def notify_bug_updated(
    bug: BugSnapshot,
    changes: list[tuple[str, str, str]],
    actor_name: str,
    actor_user_id: int | None,
) -> None:
    if _digest_owns_work_item_email():
        return
    if not changes:
        return
    to = _recipients(bug, exclude_user_id=actor_user_id)
    if not to:
        return
    label = _item_label(bug)
    label_cap = (bug.item_type or "Bug").capitalize()
    subject = f"[Bug Hunter] {label_cap} #{bug.id} updated: {bug.title}"
    lines = [f"{actor_name} updated {label} #{bug.id}.", "", "Changes:"]
    for field, old, new in changes:
        lines.append(f"  • {field}: {old or '(empty)'} → {new or '(empty)'}")
    lines += [""] + _bug_meta_lines(bug)
    lines += ["", f"View: {_bug_link(bug.id)}"]
    deliver(subject, to, "\n".join(lines))


def notify_assignment(
    bug: BugSnapshot,
    newly_assigned: Iterable[UserSnapshot],
    actor_name: str,
) -> None:
    """Send a personalized 'you've been assigned' email to each new assignee."""
    if _digest_owns_work_item_email():
        return
    label = _item_label(bug)
    for user in newly_assigned:
        if not user.email:
            continue
        subject = f"[Bug Hunter] You've been assigned to {label} #{bug.id}: {bug.title}"
        lines = [
            f"Hi {user.name},",
            "",
            f"{actor_name} assigned you to a {label}.",
            "",
        ]
        lines += _bug_meta_lines(bug)
        if bug.description:
            lines += ["", _DESC_LABEL, bug.description]
        lines += ["", f"View: {_bug_link(bug.id)}"]
        deliver(subject, [user.email], "\n".join(lines))


def notify_comment_added(
    bug: BugSnapshot,
    comment_author_name: str,
    comment_author_id: int | None,
    comment_body: str,
) -> None:
    if _digest_owns_work_item_email():
        return
    to = _recipients(bug, exclude_user_id=comment_author_id)
    if not to:
        return
    label = _item_label(bug)
    subject = f"[Bug Hunter] New comment on {label} #{bug.id}: {bug.title}"
    lines = [
        f"{comment_author_name} commented on {label} #{bug.id}:",
        "",
        comment_body,
        "",
        "---",
    ]
    lines += _bug_meta_lines(bug)
    lines += ["", f"View: {_bug_link(bug.id)}"]
    deliver(subject, to, "\n".join(lines))


# ---------------------------------------------------------------------------
# Event notifications (separate channel from per-item assignment emails)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EventSnapshot:
    """Snapshot of an event for emails. Decoupled from the ORM so the
    BackgroundTask thread can use it after the request session closes.
    """
    id: int
    name: str
    description: str
    scheduled_for: str | None
    managers: tuple[UserSnapshot, ...]


def _event_recipients(ev: EventSnapshot, exclude_user_id: int | None) -> list[str]:
    """Recipients of an event email — every manager except the actor."""
    out: list[str] = []
    seen: dict[str, None] = {}
    for u in ev.managers:
        if exclude_user_id is not None and u.id == exclude_user_id:
            continue
        if not u.email:
            continue
        key = u.email.lower()
        if key in seen:
            continue
        seen[key] = None
        out.append(u.email)
    return out


def _event_meta_lines(ev: EventSnapshot) -> list[str]:
    return [
        f"Event #{ev.id}: {ev.name}",
        f"Scheduled: {ev.scheduled_for or '—'}",
        "Managers:  " + (
            ", ".join(m.display for m in ev.managers) if ev.managers else "—"
        ),
    ]


def _event_link(event_id: int) -> str:
    base = get_settings().APP_BASE_URL.rstrip("/")
    return f"{base}/#event={event_id}"


def notify_event_created(
    ev: EventSnapshot,
    actor_name: str,
    actor_user_id: int | None,
) -> None:
    if _digest_owns_work_item_email():
        return
    to = _event_recipients(ev, exclude_user_id=actor_user_id)
    if not to:
        return
    subject = f"[Bug Hunter] New event #{ev.id}: {ev.name}"
    lines = [
        f"{actor_name} created a new event you're managing.",
        "",
    ]
    lines += _event_meta_lines(ev)
    if ev.description:
        lines += ["", _DESC_LABEL, ev.description]
    lines += ["", f"View: {_event_link(ev.id)}"]
    deliver(subject, to, "\n".join(lines))


def notify_event_updated(
    ev: EventSnapshot,
    changes: list[tuple[str, str, str]],
    actor_name: str,
    actor_user_id: int | None,
) -> None:
    if _digest_owns_work_item_email():
        return
    if not changes:
        return
    to = _event_recipients(ev, exclude_user_id=actor_user_id)
    if not to:
        return
    subject = f"[Bug Hunter] Event #{ev.id} updated: {ev.name}"
    lines = [f"{actor_name} updated event #{ev.id}.", "", "Changes:"]
    for field, old, new in changes:
        lines.append(f"  • {field}: {old or '(empty)'} → {new or '(empty)'}")
    lines += [""] + _event_meta_lines(ev)
    lines += ["", f"View: {_event_link(ev.id)}"]
    deliver(subject, to, "\n".join(lines))


def notify_event_deleted(
    ev: EventSnapshot,
    actor_name: str,
    actor_user_id: int | None,
) -> None:
    if _digest_owns_work_item_email():
        return
    to = _event_recipients(ev, exclude_user_id=actor_user_id)
    if not to:
        return
    subject = f"[Bug Hunter] Event #{ev.id} deleted: {ev.name}"
    lines = [
        f"{actor_name} deleted event #{ev.id}: {ev.name}.",
        "",
        "Any items that belonged to this event are preserved as standalone work items.",
    ]
    deliver(subject, to, "\n".join(lines))


def notify_password_reset(email: str, name: str, reset_url: str) -> None:
    """Send the user a password-reset link."""
    if not email:
        return
    subject = "[Bug Hunter] Reset your password"
    body = "\n".join([
        f"Hi {name or 'there'},",
        "",
        "We received a request to reset your Bug Hunter password.",
        "Click the link below to choose a new one. The link is valid for 2 hours.",
        "",
        reset_url,
        "",
        "If you didn't request this, you can ignore this email — your password "
        "won't change unless someone uses the link.",
        "",
        "— Bug Hunter",
    ])
    deliver(subject, [email], body)
