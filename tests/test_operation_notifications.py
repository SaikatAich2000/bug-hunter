"""Every mutating operation on a work item notifies its stakeholders.

The product rule is that all operations must trigger a notification (and feed
the email digest) to the assignees. The create / update / assign / comment-add
paths are covered in test_notifications.py; this file covers the remaining
operations:

  - link add / remove (both endpoints' assignees, not just the source's)
  - attachment add / delete
  - comment edit / delete
  - bug delete, single and bulk (notification survives the row via a NULL
    bug_id deep-link)

Plus one end-to-end digest check: with EMAIL_DIGEST_ENABLED, one of these
operations is batched into the daily digest email for the assignee.

All assertions read the Notification table directly (the notify layer commits
synchronously on the request session), so no second login is needed;
recipients are isolated per-test by a unique assignee email.
"""
from __future__ import annotations

BOOTSTRAP_EMAIL = "admin@test.local"
_PW = "User12345"  # non-secret test credential reused by the helper below


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _session():
    from app.database import SessionLocal
    return SessionLocal()


def _user_id(db, email: str) -> int:
    from sqlalchemy import select
    from app.models import User
    return db.scalar(select(User).where(User.email == email)).id


def _notifs_for(db, user_id: int):
    from sqlalchemy import select
    from app.models import Notification
    return list(db.scalars(
        select(Notification).where(Notification.user_id == user_id)
    ).all())


def _titles_containing(db, user_id: int, needle: str):
    return [n for n in _notifs_for(db, user_id) if needle in n.title]


def _setup(admin_client, suffix: str):
    """A project + a fresh assignee (Bob) + a bug reported by admin, assigned to
    Bob. Returns (project, bob, bug). Admin is the actor/reporter throughout, so
    every stakeholder notification should land on Bob and never on admin."""
    proj = admin_client.post("/api/projects", json={"name": f"Op Proj {suffix}"}).json()
    bob = admin_client.post("/api/users", json={
        "name": "Bob", "email": f"bob_{suffix}@op.test",
        "role": "user", "password": _PW,
    }).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": f"Op bug {suffix}",
        "priority": "Medium", "environment": "DEV", "assignee_ids": [bob["id"]],
    }).json()
    return proj, bob, bug


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------
def test_link_add_notifies_assignee_not_actor(admin_client):
    proj, bob, bug = _setup(admin_client, "linkadd")
    other = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Link target",
    }).json()

    r = admin_client.post(f"/api/bugs/{bug['id']}/links", json={
        "target_bug_id": other["id"], "link_type": "relates",
    })
    assert r.status_code == 201, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Link added"), "assignee not notified of link"
    admin_id = _user_id(db, BOOTSTRAP_EMAIL)
    assert not _titles_containing(db, admin_id, "Link added"), "actor should not self-notify"


def test_link_remove_notifies_assignee(admin_client):
    proj, bob, bug = _setup(admin_client, "linkrm")
    other = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Link target 2",
    }).json()
    link = admin_client.post(f"/api/bugs/{bug['id']}/links", json={
        "target_bug_id": other["id"], "link_type": "blocks",
    }).json()

    r = admin_client.delete(f"/api/bugs/{bug['id']}/links/{link['id']}")
    assert r.status_code == 200, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Link removed"), "assignee not notified of unlink"


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
def test_attachment_add_notifies_assignee(admin_client):
    _proj, bob, bug = _setup(admin_client, "attadd")
    r = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("note.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 201, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Attachment added"), "assignee not notified of attach"


def test_attachment_delete_notifies_assignee(admin_client):
    _proj, bob, bug = _setup(admin_client, "attdel")
    att = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("note.txt", b"bye", "text/plain")},
    ).json()

    r = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/{att['id']}")
    assert r.status_code == 200, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Attachment removed"), "assignee not notified of detach"


# ---------------------------------------------------------------------------
# Comment edit / delete (comment ADD is covered in test_notifications.py)
# ---------------------------------------------------------------------------
def test_comment_edit_notifies_assignee(admin_client):
    _proj, bob, bug = _setup(admin_client, "cedit")
    c = admin_client.post(f"/api/bugs/{bug['id']}/comments",
                          json={"body": "<p>first</p>"}).json()

    r = admin_client.put(f"/api/bugs/{bug['id']}/comments/{c['id']}",
                         json={"body": "<p>edited</p>"})
    assert r.status_code == 200, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Comment edited"), "assignee not notified of edit"


def test_comment_delete_notifies_assignee(admin_client):
    _proj, bob, bug = _setup(admin_client, "cdel")
    c = admin_client.post(f"/api/bugs/{bug['id']}/comments",
                          json={"body": "<p>to be gone</p>"}).json()

    r = admin_client.delete(f"/api/bugs/{bug['id']}/comments/{c['id']}")
    assert r.status_code == 200, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Comment deleted"), "assignee not notified of delete"


# ---------------------------------------------------------------------------
# Bug delete — notification must outlive the row (NULL bug_id, no FK cascade)
# ---------------------------------------------------------------------------
def test_bug_delete_notifies_assignee_and_survives(admin_client):
    _proj, bob, bug = _setup(admin_client, "bugdel")

    r = admin_client.delete(f"/api/bugs/{bug['id']}")
    assert r.status_code == 200, r.text

    db = _session()
    deleted = [n for n in _notifs_for(db, bob["id"]) if "deleted" in n.title.lower()]
    assert deleted, "assignee not notified of bug delete"
    # The deep-link is dropped so the CASCADE FK can't erase the notification.
    assert deleted[0].bug_id is None


# ---------------------------------------------------------------------------
# Digest inclusion — a new operation gets batched into the daily email
# ---------------------------------------------------------------------------
def test_new_operation_is_batched_into_digest(admin_client, monkeypatch):
    from app.config import get_settings
    # Digest ON → notify() leaves emailed_at NULL so the job owns the email.
    monkeypatch.setattr(get_settings(), "EMAIL_DIGEST_ENABLED", True)

    sent: list[tuple[str, list[str], str]] = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body: sent.append((subject, to, body)),
    )

    proj, bob, bug = _setup(admin_client, "digest")
    other = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Digest link target",
    }).json()
    r = admin_client.post(f"/api/bugs/{bug['id']}/links", json={
        "target_bug_id": other["id"], "link_type": "relates",
    })
    assert r.status_code == 201, r.text

    from app.jobs.email_digest import run_digest
    db = _session()
    stats = run_digest(db)
    db.commit()
    assert stats["emails_sent"] >= 1

    bob_emails = [body for _subj, to, body in sent if bob["email"] in to]
    assert bob_emails, f"no digest email reached the assignee; sent={sent}"
    assert any("Link added" in body for body in bob_emails), bob_emails


# ---------------------------------------------------------------------------
# Bulk delete — must notify exactly like a single delete (was previously silent)
# ---------------------------------------------------------------------------
def test_bulk_delete_notifies_assignee_and_survives(admin_client):
    _proj, bob, bug = _setup(admin_client, "bulkdel")

    r = admin_client.post("/api/bugs/bulk", json={
        "action": "delete", "ids": [bug["id"]],
    })
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1

    db = _session()
    deleted = [n for n in _notifs_for(db, bob["id"]) if "deleted" in n.title.lower()]
    assert deleted, "assignee not notified of bulk delete"
    # Deep-link dropped (link_bug=False) so the CASCADE FK can't erase the row.
    assert deleted[0].bug_id is None


# ---------------------------------------------------------------------------
# Link notifies both endpoints: the target item's assignees are notified too,
# since a link (a "blocks" edge especially) changes the target item as much as
# the source.
# ---------------------------------------------------------------------------
def test_link_add_notifies_both_endpoint_assignees(admin_client):
    proj, bob, bug = _setup(admin_client, "linkboth")
    carol = admin_client.post("/api/users", json={
        "name": "Carol", "email": "carol_linkboth@op.test",
        "role": "user", "password": _PW,
    }).json()
    target = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Target with owner",
        "priority": "Medium", "environment": "DEV", "assignee_ids": [carol["id"]],
    }).json()

    r = admin_client.post(f"/api/bugs/{bug['id']}/links", json={
        "target_bug_id": target["id"], "link_type": "blocks",
    })
    assert r.status_code == 201, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Link added"), "source assignee not notified"
    assert _titles_containing(db, carol["id"], "Link added"), "target assignee not notified"
