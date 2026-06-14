"""Tests for the daily email-digest job (app/jobs/email_digest.py).

Covers:
  - one grouped email per user, operations bucketed by category
  - idempotency (a second run sends nothing — rows are stamped)
  - the lookback window (old operations age out instead of replaying)
  - undeliverable recipients are skipped and left un-stamped
  - main() is a no-op when EMAIL_DIGEST_ENABLED is off, and sends when on
  - with the digest ON the per-operation work-item emails are suppressed,
    while the password-reset email STILL sends immediately (security email)

All direct-DB: we seed notification rows on a real session and call the job
in-process, capturing email_service.deliver(...) so no SMTP is touched.
"""
from __future__ import annotations

from datetime import timedelta

BOOTSTRAP_EMAIL = "admin@test.local"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _capture_deliver(monkeypatch) -> list[tuple[str, list[str], str]]:
    """Patch email_service.deliver to record (subject, to, body) calls."""
    sent: list[tuple[str, list[str], str]] = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body: sent.append((subject, to, body)),
    )
    return sent


def _session():
    from app.database import SessionLocal
    return SessionLocal()


def _user_id(db, email: str) -> int:
    from sqlalchemy import select
    from app.models import User
    return db.scalar(select(User).where(User.email == email)).id


def _mk_user(admin_client, name: str, email: str) -> int:
    r = admin_client.post("/api/users", json={
        "name": name, "email": email, "role": "user", "password": "User12345",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _add_notif(db, user_id, kind, title, *, body="", bug_id=None,
               age_hours=1, emailed=False):
    from app.models import Notification, _utcnow
    now = _utcnow()
    n = Notification(
        user_id=user_id, kind=kind, title=title, body=body,
        bug_id=bug_id, actor_name="Actor",
    )
    n.created_at = now - timedelta(hours=age_hours)
    if emailed:
        n.emailed_at = now
    db.add(n)
    db.flush()
    return n


def _enable_digest(monkeypatch, on=True):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "EMAIL_DIGEST_ENABLED", on)


# ---------------------------------------------------------------------------
# run_digest
# ---------------------------------------------------------------------------
def test_one_grouped_email_per_user_then_idempotent(admin_client, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    uid2 = _mk_user(admin_client, "Dev Two", "dev2@test.local")

    db = _session()
    from app.models import _utcnow
    from app.jobs.email_digest import run_digest
    admin_id = _user_id(db, BOOTSTRAP_EMAIL)
    _add_notif(db, admin_id, "assigned", "Assigned to Bug #1", body="x assigned you")
    _add_notif(db, admin_id, "comment", "New comment on Bug #1", body="x commented")
    _add_notif(db, uid2, "updated", "Bug #2 updated", body="status changed")
    db.commit()

    stats = run_digest(db, now=_utcnow())
    assert stats == {"users": 2, "emails_sent": 2, "operations": 3}
    assert len(sent) == 2

    admin_body = next(b for (_s, to, b) in sent if BOOTSTRAP_EMAIL in to)
    # Two distinct categories present, grouped, in one email.
    assert "Assigned to you" in admin_body
    assert "Comments" in admin_body
    assert "Bug #1" in admin_body

    # Second run sends nothing — the rows were stamped emailed_at.
    sent.clear()
    stats2 = run_digest(db, now=_utcnow())
    assert stats2["emails_sent"] == 0
    assert sent == []
    db.close()


def test_lookback_window_excludes_old_operations(admin_client, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    db = _session()
    from app.models import _utcnow
    from app.jobs.email_digest import run_digest
    admin_id = _user_id(db, BOOTSTRAP_EMAIL)
    _add_notif(db, admin_id, "updated", "recent op", age_hours=1)
    _add_notif(db, admin_id, "updated", "ancient op", age_hours=48)
    db.commit()

    stats = run_digest(db, now=_utcnow(), lookback_hours=26)
    assert stats["operations"] == 1
    assert len(sent) == 1
    assert "recent op" in sent[0][2]
    assert "ancient op" not in sent[0][2]
    db.close()


def test_inactive_user_is_skipped_unstamped(admin_client, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    uid = _mk_user(admin_client, "Gone Dev", "gone@test.local")

    db = _session()
    from app.models import User, _utcnow
    from app.jobs.email_digest import run_digest
    # Deactivate the user — a deactivated account must not receive a digest.
    user = db.get(User, uid)
    user.is_active = False
    db.flush()
    row = _add_notif(db, uid, "updated", "to a deactivated dev")
    db.commit()

    stats = run_digest(db, now=_utcnow())
    assert stats["emails_sent"] == 0
    assert sent == []
    # Left un-stamped so nothing is lost — it just ages out of the window.
    db.refresh(row)
    assert row.emailed_at is None
    db.close()


# ---------------------------------------------------------------------------
# main() entry point + the global toggle
# ---------------------------------------------------------------------------
def test_main_is_noop_when_disabled(admin_client, monkeypatch):
    _enable_digest(monkeypatch, on=False)
    sent = _capture_deliver(monkeypatch)
    db = _session()
    admin_id = _user_id(db, BOOTSTRAP_EMAIL)
    _add_notif(db, admin_id, "assigned", "Assigned to Bug #9", body="x")
    db.commit()
    db.close()

    from app.jobs.email_digest import main
    assert main() == 0
    assert sent == []  # disabled → nothing sent, rows wait for an enabled run


def test_main_sends_when_enabled(admin_client, monkeypatch):
    _enable_digest(monkeypatch, on=True)
    sent = _capture_deliver(monkeypatch)
    db = _session()
    admin_id = _user_id(db, BOOTSTRAP_EMAIL)
    _add_notif(db, admin_id, "assigned", "Assigned to Bug #9", body="x assigned you")
    db.commit()
    db.close()

    from app.jobs.email_digest import main
    assert main() == 0
    assert len(sent) == 1
    assert "activity digest" in sent[0][0].lower()


# ---------------------------------------------------------------------------
# Suppression of immediate work-item emails — but NOT security emails
# ---------------------------------------------------------------------------
def test_digest_on_suppresses_work_item_email_but_not_password_reset(
    admin_client, monkeypatch,
):
    _enable_digest(monkeypatch, on=True)
    sent = _capture_deliver(monkeypatch)
    from app import email_service

    snap = email_service.BugSnapshot(
        id=1, title="A bug", project_name="P", status="New", priority="Medium",
        environment="DEV", description="",
        reporter=email_service.UserSnapshot(1, "Reporter", "rep@test.local"),
        assignees=(),
    )
    # Operation email is suppressed (the digest will carry it instead).
    email_service.notify_bug_created(snap, actor_user_id=None)
    assert sent == []

    # Security/transactional email is NOT suppressed — it must send now.
    email_service.notify_password_reset("user@test.local", "User", "https://x/reset")
    assert len(sent) == 1
    assert "Reset your password" in sent[0][0]


def test_immediate_era_operations_are_never_later_digested(admin_client, monkeypatch):
    """The 'only new, never old' guarantee: an operation recorded while the
    digest was OFF is born already-emailed (emailed_at stamped), so turning the
    digest ON later never re-sends it."""
    _enable_digest(monkeypatch, on=False)  # immediate mode when the op happens
    from app import notification_service
    from app.models import _utcnow
    from app.jobs.email_digest import run_digest

    db = _session()
    admin_id = _user_id(db, BOOTSTRAP_EMAIL)
    notification_service.notify(
        db, [admin_id], kind="updated", title="immediate-era op",
    )
    db.commit()

    # Now the digest runs (e.g. someone enabled it afterwards) — nothing to send.
    sent = _capture_deliver(monkeypatch)
    stats = run_digest(db, now=_utcnow())
    assert stats["emails_sent"] == 0
    assert sent == []
    db.close()


def test_digest_era_operations_are_picked_up(admin_client, monkeypatch):
    """Counterpart: an operation recorded while the digest is ON is left
    un-emailed (emailed_at NULL) and the daily job carries it."""
    _enable_digest(monkeypatch, on=True)
    from app import notification_service
    from app.models import _utcnow
    from app.jobs.email_digest import run_digest

    db = _session()
    admin_id = _user_id(db, BOOTSTRAP_EMAIL)
    notification_service.notify(
        db, [admin_id], kind="assigned", title="digest-era op", body="x assigned you",
    )
    db.commit()

    sent = _capture_deliver(monkeypatch)
    stats = run_digest(db, now=_utcnow())
    assert stats["emails_sent"] == 1
    assert "digest-era op" in sent[0][2]
    db.close()


def test_digest_off_keeps_immediate_work_item_email(admin_client, monkeypatch):
    _enable_digest(monkeypatch, on=False)
    sent = _capture_deliver(monkeypatch)
    from app import email_service

    snap = email_service.BugSnapshot(
        id=2, title="Another bug", project_name="P", status="New",
        priority="Medium", environment="DEV", description="",
        reporter=email_service.UserSnapshot(1, "Reporter", "rep@test.local"),
        assignees=(),
    )
    email_service.notify_bug_created(snap, actor_user_id=None)
    # Default behaviour preserved: the immediate email still goes out.
    assert len(sent) == 1
    assert "New bug #2" in sent[0][0]
