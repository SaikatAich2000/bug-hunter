"""ORM models for Bug Hunter.

Tables:
  - users            : team members
  - projects         : workspaces
  - bugs             : core work-item entity
  - bug_assignees    : many-to-many between bugs and users
  - comments         : threaded discussion on a bug
  - attachments      : file blobs (PDF / image / video) attached to a bug
                       or to a comment. Stored inside the database so they
                       persist across restarts and survive backups.
  - activity_log     : audit trail
  - password_reset_tokens : single-use email-based password reset tokens
  - sessions         : server-side record of every active login. Lets admins
                       see who's currently signed in and revoke individual
                       sessions to log a specific device out without
                       affecting any others.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
)
from sqlalchemy.orm import Mapped, deferred, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# FK target strings, extracted to module-level constants to avoid repeating
# the same literal across every ForeignKey() call.
_FK_BUGS_ID = "bugs.id"
_FK_USERS_ID = "users.id"
_FK_PROJECTS_ID = "projects.id"
_FK_COMMENTS_ID = "comments.id"
_FK_EVENTS_ID = "events.id"

# SQLAlchemy relationship cascade and ondelete keywords — same reason.
_CASCADE_ALL_DELETE_ORPHAN = "all, delete-orphan"
_ONDELETE_SET_NULL = "SET NULL"


# ---------------------------------------------------------------------------
# Junctions
# ---------------------------------------------------------------------------
bug_assignees = Table(
    "bug_assignees",
    Base.metadata,
    Column("bug_id", Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), primary_key=True),
)
# user_id is the trailing column of the (bug_id, user_id) PK, so Postgres can't
# seek on it alone. A standalone index backs "all bugs assigned to user X"
# joins (stats by-assignee, event assignee counts). Added additively by
# init_db's _add_missing_indexes.
Index("idx_bug_assignees_user_id", bug_assignees.c.user_id)

# event_managers: many-to-many between events and the (admin/manager) users
# who own that event. Notifications about the event (create / edit / delete)
# fan out to this list. Tasks created inside the event email their own
# assignees as normal and do not cc the event managers, so adding someone here
# doesn't sign them up for every task email.
event_managers = Table(
    "event_managers",
    Base.metadata,
    Column("event_id", Integer, ForeignKey(_FK_EVENTS_ID, ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), primary_key=True),
)
# As with bug_assignees, user_id is the trailing column of the (event_id,
# user_id) PK, so Postgres can't seek on it alone. A standalone index backs
# "events managed by user X" lookups and the ON DELETE CASCADE that fires when
# a user is deleted. Added additively by init_db's _add_missing_indexes.
Index("idx_event_managers_user_id", event_managers.c.user_id)


# ---------------------------------------------------------------------------
# User
#
# Roles (enforced in app code, not DB constraints, for flexibility):
#   admin    - full access; only admins manage users and delete anything
#   manager  - can edit any work item or project, but not users
#   user     - default. The tracker is collaborative: a user can edit any Bug
#              (not only their own), matching the flat read model where everyone
#              already sees every item. Requirements and Tasks are planning
#              artifacts and stay manager/admin-only; all deletes are
#              admin-only. The authoritative rules live in app/auth.py
#              (can_edit_bug / can_delete_bug); keep this comment in sync.
# ---------------------------------------------------------------------------
ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_USER = "user"
VALID_ROLES = (ROLE_ADMIN, ROLE_MANAGER, ROLE_USER)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_USER)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # bcrypt hash of password. Nullable to support the unlikely case of
    # SSO integration later, but normally always set.
    password_hash: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Bumped on password change / reset / forced logout. Sessions baked
    # with an old session_version no longer validate. This is what makes
    # "I changed my password" actually log out other devices.
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    # Case-insensitive email uniqueness is enforced at the app layer (see
    # app/routes/users.py `_email_in_use`): every email is lowercased before
    # insert and a func.lower() lookup also rejects a collision with any legacy
    # mixed-case row. A DB-level expression index was avoided because SQLite
    # can't reflect one, which breaks the additive-index idempotency check.
    __table_args__ = (Index("idx_users_email", "email"),)


# ---------------------------------------------------------------------------
# PasswordResetToken
#
# Single-use tokens emailed to users to reset a forgotten password.
# Stored as a sha256 hash; never the plaintext.
# ---------------------------------------------------------------------------
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("idx_prt_token_hash", "token_hash"),
        # Covers invalidate_outstanding_reset_tokens' WHERE (user_id, used_at)
        # run on every password change/reset, and the ON DELETE CASCADE on user
        # delete. Additive — created by init_db's _add_missing_indexes.
        Index("idx_prt_user_id_used_at", "user_id", "used_at"),
    )


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------
class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="#c9764f")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    bugs: Mapped[list["Bug"]] = relationship(
        "Bug", back_populates="project", cascade=_CASCADE_ALL_DELETE_ORPHAN
    )


# ---------------------------------------------------------------------------
# Event
#
# Container for a group of work items, typically a standup / sprint meeting.
# Items (Bug / Requirement / Task) point at an event via the optional
# `event_id` FK on the bugs table. An item can exist standalone (event_id
# NULL) or be added to an event later by setting the FK.
#
# Deleting an event sets all linked items' event_id to NULL — the items
# themselves are preserved so the audit trail and assignee work isn't lost.
# ---------------------------------------------------------------------------
class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # YYYY-MM-DD; matches the date-only format used for Bug.due_date so
    # filtering by date is consistent across the app.
    scheduled_for: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    items: Mapped[list["Bug"]] = relationship(
        "Bug", back_populates="event",
        # Don't cascade-delete items when an event goes away — see module
        # docstring above. We null out the FK in app code on event delete
        # so the items survive.
        passive_deletes=True,
    )
    managers: Mapped[list["User"]] = relationship(
        "User", secondary=event_managers, lazy="selectin",
    )

    __table_args__ = (
        Index("idx_events_scheduled_for", "scheduled_for"),
        Index("idx_events_created_by", "created_by_user_id"),
    )


# ---------------------------------------------------------------------------
# Bug (= work item — Bug / Requirement / Task)
# ---------------------------------------------------------------------------
class Bug(Base):
    __tablename__ = "bugs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    reporter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    # Optional link to an event (standup / sprint meeting). Nullable so a
    # work item can exist independently. Delete behaviour: when the event
    # row is deleted, the FK is NULLed via SQL (SET NULL), preserving the
    # work item itself.
    event_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_EVENTS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Work-item type. The table is still called "bugs" for backwards
    # compatibility (and to keep the production database safe on upgrade),
    # but each row can now be a Bug, a Requirement (Jira-style story / spec)
    # or a Task (daily standup work item). Default "Bug" so any pre-existing
    # row created before this column existed is interpreted correctly.
    item_type: Mapped[str] = mapped_column(String(20), nullable=False, default="Bug")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="New")
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="Medium")
    # environment is now restricted to DEV / UAT / PROD (enforced in schemas)
    environment: Mapped[str] = mapped_column(String(10), nullable=False, default="DEV")
    due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    # Optimistic-concurrency counter — bumped on every committed update so a
    # client editing a stale copy is caught at sub-second resolution (the
    # whole-second updated_at alone can miss same-second collisions). Fresh rows
    # and any legacy row migrated by _add_missing_columns start at 1.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    project: Mapped[Project] = relationship("Project", back_populates="bugs")
    reporter: Mapped["User | None"] = relationship("User", foreign_keys=[reporter_id])
    event: Mapped["Event | None"] = relationship("Event", back_populates="items")
    assignees: Mapped[list["User"]] = relationship(
        "User", secondary=bug_assignees, lazy="selectin"
    )
    comments: Mapped[list["Comment"]] = relationship(
        "Comment", back_populates="bug", cascade=_CASCADE_ALL_DELETE_ORPHAN,
        # Newest comment first. This order_by drives every access via
        # Bug.comments (get_bug response, etc.).
        order_by="(Comment.created_at.desc(), Comment.id.desc())",
    )
    # Activities ordered newest-first with an id-DESC tiebreaker so two rows
    # recorded in the same second still come back in a stable, insert-
    # consistent order that matches the dedicated /activity endpoint.
    # Activity rows are not cascade-deleted when a bug is deleted: the audit
    # trail must outlive the bugs it describes so an admin can still find "who
    # deleted bug #42 and when". On delete the route handler detaches the rows
    # (bug_id → NULL); entity_id stays set to the original bug id and the detail
    # string preserves the title, so the global audit screen shows the full
    # history.
    activities: Mapped[list["Activity"]] = relationship(
        "Activity", back_populates="bug",
        order_by="(Activity.created_at.desc(), Activity.id.desc())",
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        "Attachment", back_populates="bug", cascade=_CASCADE_ALL_DELETE_ORPHAN,
        order_by="Attachment.created_at.desc()",
        primaryjoin="Bug.id == Attachment.bug_id",
    )

    __table_args__ = (
        Index("idx_bugs_project_id", "project_id"),
        Index("idx_bugs_reporter_id", "reporter_id"),
        Index("idx_bugs_status", "status"),
        Index("idx_bugs_priority", "priority"),
        Index("idx_bugs_environment", "environment"),
        Index("idx_bugs_item_type", "item_type"),
        Index("idx_bugs_item_type_status", "item_type", "status"),
        Index("idx_bugs_event_id", "event_id"),
        # Additive composite indexes for the common dashboard queries that
        # filter on multiple columns at once. The single-column indexes above
        # still serve queries that filter on just one field. create_all() is
        # idempotent, so adding these on a live DB leaves existing data and
        # indexes untouched.
        Index("idx_bugs_project_status", "project_id", "status"),
        Index("idx_bugs_status_priority", "status", "priority"),
        Index("idx_bugs_updated_at", "updated_at"),
        # The default list page orders by (updated_at DESC, id DESC); this
        # composite lets the database satisfy that ordering from the index and
        # skip a sort on large tables.
        Index("idx_bugs_updated_id", "updated_at", "id"),
        # The stats timeline (GROUP BY date(created_at)) and the "oldest first"
        # report orderings scan on created_at. Additive; created by init_db's
        # index reconciliation on next boot.
        Index("idx_bugs_created_at", "created_at"),
    )


# ---------------------------------------------------------------------------
# Comment
# ---------------------------------------------------------------------------
class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=False)
    author_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    author_name: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    bug: Mapped[Bug] = relationship("Bug", back_populates="comments")

    __table_args__ = (Index("idx_comments_bug_id", "bug_id"),)


# ---------------------------------------------------------------------------
# Attachment
#
# Files (PDF / image / video) are stored INSIDE the database as a BLOB.
# This is intentional:
#   - No NFS / S3 / object-store dependency.
#   - One backup of the database is a full backup of all attachments.
#   - Survives container restart and host migration.
#
# Trade-off: very large videos can bloat the DB. Upload size is capped at
# 50 MB per file (config-driven). To outgrow this, move data to S3 with no API
# change, only the storage layer.
#
# An attachment can belong to a bug directly (bug_id set, comment_id NULL)
# or to a comment (both set; comment FK lives in addition to bug FK so a
# bug-level query still finds it).
# ---------------------------------------------------------------------------
class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=False
    )
    comment_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_COMMENTS_ID, ondelete="CASCADE"), nullable=True
    )
    uploader_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    uploader_name: Mapped[str] = mapped_column(String(120), nullable=False, default="anonymous")
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Deferred: the BLOB is fetched only when .data is touched, so listing
    # attachments for a bug or comment doesn't pull the full file content from
    # the DB. The download endpoint still works because accessing `a.data`
    # lazily issues a single SELECT for the bytes. Python-side loading
    # strategy only — the schema is unchanged.
    data: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    bug: Mapped[Bug] = relationship("Bug", back_populates="attachments", foreign_keys=[bug_id])

    __table_args__ = (
        Index("idx_attachments_bug_id", "bug_id"),
        Index("idx_attachments_comment_id", "comment_id"),
        # Composite supports the per-bug "load all attachments and split into
        # bug-level vs by-comment" pattern in routes/bugs.py.
        Index("idx_attachments_bug_comment", "bug_id", "comment_id"),
    )


# ---------------------------------------------------------------------------
# Activity (audit trail)
#
# `bug_id` is nullable so non-bug events (user created, project deleted, etc.)
# can also be logged for the global audit-trail screen.
# ---------------------------------------------------------------------------
class Activity(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # bug_id is SET NULL on delete (not CASCADE) so audit history survives
    # when a bug is deleted. The row keeps its entity_type / entity_id
    # pointing at the (now gone) bug, and the detail string preserves the
    # title — so searching the audit trail for that bug still works.
    bug_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    # entity_type + entity_id let us reference any object: "user", "project",
    # "bug", "comment", "attachment". Lightweight — no FK, just metadata.
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, default="bug")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    actor_name: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    bug: Mapped[Bug | None] = relationship("Bug", back_populates="activities")

    __table_args__ = (
        Index("idx_activity_bug_id", "bug_id"),
        Index("idx_activity_entity", "entity_type", "entity_id"),
        Index("idx_activity_created", "created_at"),
        # Reports filter `action == "status_changed"` joined to bugs
        # (throughput / time-to-resolution / timeline); without this they scan
        # the whole ever-growing audit table. The audit "filter by actor"
        # screen filters actor_user_id. Both added additively by init_db().
        Index("idx_activity_action_bug", "action", "bug_id"),
        # The resolution/throughput/timeline reports order the matched rows by
        # (bug_id, created_at); extending the action index with created_at lets
        # those reports avoid an in-memory sort over the audit table.
        Index("idx_activity_action_bug_created", "action", "bug_id", "created_at"),
        # Those same reports filter `action == "status_changed"` AND a
        # created_at *range* without constraining bug_id. The (action, bug_id,
        # created_at) index above can't seek the date range (bug_id sits between
        # the two predicates), so this (action, created_at) index makes the
        # date-windowed scan sargable as the audit table grows. Additive.
        Index("idx_activity_action_created", "action", "created_at"),
        Index("idx_activity_actor_user_id", "actor_user_id"),
    )


# ---------------------------------------------------------------------------
# Session
#
# Server-side record of every active login, needed for the "list / revoke
# active sessions" admin feature: stateless signed cookies alone can't be
# selectively revoked because the server keeps no record of which tokens it
# has issued.
#
# Each session row is keyed by a unique `jti` (JWT-style ID) also baked into
# the signed session cookie. On every authenticated request the row is looked
# up by jti; if it's missing or expired, the cookie is rejected. The admin
# "revoke" action deletes the row.
#
# Tokens issued before this table existed don't carry a jti. The auth layer
# accepts those tokens but they don't appear in the session list until the
# user next logs in.
# ---------------------------------------------------------------------------
class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    # Random opaque ID baked into the signed cookie. We look up sessions
    # by this on every authenticated request.
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Metadata for the admin session-list screen. Never used for auth
    # decisions — purely informational.
    user_agent: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_sessions_jti", "jti"),
        Index("idx_sessions_user_id", "user_id"),
        Index("idx_sessions_expires_at", "expires_at"),
    )


# ---------------------------------------------------------------------------
# Notification
#
# Per-user in-app notifications — the in-app counterpart to the emails the
# user already receives. Recipients mirror email_service exactly (reporter +
# assignees minus the actor, event managers, etc.), so a notification is only
# ever written for a user already entitled to know about the event, and no
# endpoint returns another user's notifications.
#
# Created by init_db()'s create_all() on next boot. Scoped to a user_id and
# cascade-deletes with the user.
# ---------------------------------------------------------------------------
class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    # Event category — drives the icon/label on the frontend:
    # "assigned" | "reported" | "updated" | "comment" | "event".
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Optional deep-links — clicking the notification opens the related item.
    # CASCADE so a notification can't outlive the bug/event it points at.
    bug_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=True
    )
    event_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_EVENTS_ID, ondelete="CASCADE"), nullable=True
    )
    # Snapshot of who triggered it — no FK, so it survives actor deletion.
    actor_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # NULL = unread; set to a timestamp when the user reads it.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL = this operation has not yet been included in a sent email digest.
    # Stamped with the send time once the daily digest job mails it out, so the
    # job is idempotent and never re-sends the same operation. Independent of
    # read_at (in-app read state) — a row can be read in-app but not yet
    # digested, or vice-versa.
    emailed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_notifications_user_id", "user_id"),
        # The unread badge + unread list both filter on (user_id, read_at).
        Index("idx_notifications_user_read", "user_id", "read_at"),
        # The panel lists a user's notifications newest-first.
        Index("idx_notifications_user_created", "user_id", "created_at"),
        # The daily digest job scans for un-emailed operations (emailed_at NULL).
        Index("idx_notifications_emailed_at", "emailed_at"),
        # …and filters that set by a created_at cutoff, oldest-first. The
        # composite lets the digest seek straight to the window instead of
        # scanning every un-emailed row. Additive; created by init_db on boot.
        Index("idx_notifications_emailed_created", "emailed_at", "created_at"),
    )


# ---------------------------------------------------------------------------
# Push subscriptions (web push via Firebase Cloud Messaging).
#
# One row per device/browser a user has granted push permission on. Stored as
# the FCM registration token so the same table and send path also serve a
# native Android app (which registers with platform="android"). Push is sent
# immediately when an operation occurs, not digested.
# ---------------------------------------------------------------------------
class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    # FCM registration token — unique per device/browser install.
    token: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    # "web" today; "android" / "ios" when native clients register here too.
    platform: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    # Coarse device hint for the user's "your devices" view / debugging.
    user_agent: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # Refreshed every time the client re-subscribes (tokens rotate); lets a
    # cleanup job drop tokens no client has touched in a long time.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_push_subscriptions_user_id", "user_id"),
        Index("idx_push_subscriptions_token", "token"),
    )


# ---------------------------------------------------------------------------
# Sleuth chat memory
#
# Persists the assistant conversation so context survives process restarts and
# so the cloud layer can be given a short rolling history. The in-process
# memory.store still handles fast per-turn referents ("it", pending
# confirmations); these tables are the durable transcript.
#
# Created by init_db()'s create_all() on next boot. Both are scoped to a
# user_id and cascade-delete with the user.
# ---------------------------------------------------------------------------
class ChatConversation(Base):
    __tablename__ = "chat_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage", back_populates="conversation",
        cascade=_CASCADE_ALL_DELETE_ORPHAN,
        # id is the tiebreaker: _utcnow() truncates to whole seconds, so a
        # user+assistant turn written in the same second would otherwise replay
        # in an unstable order. Matches the (created_at, id) ordering used by
        # Bug.comments / Bug.activities.
        order_by="ChatMessage.created_at, ChatMessage.id",
    )

    __table_args__ = (Index("idx_chat_conv_user_id", "user_id"),)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    # "user" | "assistant"
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Which engine answered: "rules" | "classifier" | "llm" | "cloud" | "" .
    # Lightweight observability; never used for auth.
    engine: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    conversation: Mapped[ChatConversation] = relationship(
        "ChatConversation", back_populates="messages"
    )

    __table_args__ = (Index("idx_chat_msg_conversation_id", "conversation_id"),)


# ---------------------------------------------------------------------------
# BugLink
#
# A directed relationship between two work items: "#12 blocks #34", "#5
# duplicates #2", "#7 relates to #9". Stored as a single directed edge
# (source -> target) with a link_type; the route layer renders the inverse
# label from the target's perspective ("is blocked by"), so no redundant
# reverse rows are stored. Both FKs CASCADE so a link can't outlive either of
# the items it connects.
# ---------------------------------------------------------------------------
class BugLink(Base):
    __tablename__ = "bug_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_bug_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=False
    )
    target_bug_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=False
    )
    # "relates" | "blocks" | "duplicate" — validated in the schema layer.
    link_type: Mapped[str] = mapped_column(String(20), nullable=False, default="relates")
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    source: Mapped["Bug"] = relationship("Bug", foreign_keys=[source_bug_id])
    target: Mapped["Bug"] = relationship("Bug", foreign_keys=[target_bug_id])

    __table_args__ = (
        # One edge per (source, target, type) — re-linking is idempotent.
        Index("idx_bug_links_unique", "source_bug_id", "target_bug_id", "link_type", unique=True),
        Index("idx_bug_links_source", "source_bug_id"),
        Index("idx_bug_links_target", "target_bug_id"),
    )
