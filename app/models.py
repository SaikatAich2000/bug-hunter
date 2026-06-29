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


# FK target strings as constants to avoid scattering the same literal
# across every ForeignKey() call.
_FK_BUGS_ID = "bugs.id"
_FK_USERS_ID = "users.id"
_FK_PROJECTS_ID = "projects.id"
_FK_COMMENTS_ID = "comments.id"
_FK_EVENTS_ID = "events.id"

# Shared cascade/ondelete strings for the same reason.
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
# user_id is the trailing PK column, so Postgres can't seek on it alone.
# This index backs "all bugs assigned to user X" queries (assignee stats,
# event counts). Added additively by init_db's _add_missing_indexes.
Index("idx_bug_assignees_user_id", bug_assignees.c.user_id)

# event_managers: many-to-many between events and the users who own them.
# Event-level notifications (create/edit/delete) go to this list. Tasks inside
# the event notify their own assignees separately, so being an event manager
# doesn't mean receiving every task notification.
event_managers = Table(
    "event_managers",
    Base.metadata,
    Column("event_id", Integer, ForeignKey(_FK_EVENTS_ID, ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), primary_key=True),
)
# Same situation as bug_assignees: user_id is the trailing PK column, so a
# standalone index is needed for "events managed by user X" lookups and for
# the ON DELETE CASCADE triggered on user deletion. Added additively.
Index("idx_event_managers_user_id", event_managers.c.user_id)

# user_projects: many-to-many controlling project-scoped access.
# Managers and regular users see only items, events, stats, reports, and audit
# entries for projects they belong to. Admins always see everything regardless.
# A user with no rows here sees nothing until an admin assigns them to a project.
# Existing accounts get zero rows after the additive migration, which is correct:
# a fresh install with no memberships is also locked down for non-admins.
# Both FKs are ON DELETE CASCADE so rows can't outlive their user or project.
# Created by init_db()'s create_all() on next boot — no existing column or row
# is touched.
user_projects = Table(
    "user_projects",
    Base.metadata,
    Column("user_id", Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), primary_key=True),
    Column("project_id", Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), primary_key=True),
)
# project_id is the trailing PK column, so a standalone index is needed for
# "members of project X" lookups and the ON DELETE CASCADE on project deletion.
# Added additively by init_db's _add_missing_indexes.
Index("idx_user_projects_project_id", user_projects.c.project_id)


# ---------------------------------------------------------------------------
# User
#
# Roles are enforced in app code (not DB constraints) for flexibility:
#   admin    - full access; only admins manage users and delete anything
#   manager  - can edit any work item or project, but not users
#   user     - default. Editing is collaborative: any user can edit any Bug
#              (not only their own), matching the flat read model. Requirements
#              and Tasks stay manager/admin-only; all deletes are admin-only.
#              Authoritative rules: app/auth.py (can_edit_bug / can_delete_bug).
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

    # bcrypt hash. Nullable to leave room for SSO integration, but normally
    # always set.
    password_hash: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Bumped on password change/reset/forced logout. A session cookie carrying
    # an older version is rejected, which logs out other devices automatically.
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    # Case-insensitive uniqueness is enforced in app code (app/routes/users.py
    # `_email_in_use`): emails are lowercased before insert and a func.lower()
    # lookup catches any legacy mixed-case collision. A DB-level expression index
    # was skipped because SQLite can't reflect one, which would break the
    # additive-index idempotency check.
    __table_args__ = (Index("idx_users_email", "email"),)


# ---------------------------------------------------------------------------
# PasswordResetToken
#
# Single-use tokens for email-based password reset. Stored as a sha256 hash,
# never plaintext.
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
        # Covers the (user_id, used_at) filter in
        # invalidate_outstanding_reset_tokens (called on every password change)
        # and the ON DELETE CASCADE on user delete. Additive.
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
# Groups work items together, typically for a standup or sprint meeting.
# Items (Bug/Requirement/Task) reference an event via the optional event_id FK
# on the bugs table and can be added to an event at any time.
#
# Deleting an event NULLs the FK on all linked items rather than deleting them,
# so the audit trail and assignee history are preserved.
# ---------------------------------------------------------------------------
class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # YYYY-MM-DD, consistent with Bug.due_date.
    scheduled_for: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Owning project. Project-restricted managers/users only see events for
    # their projects. Nullable so the additive migration requires no backfill
    # (existing events are visible to admins only until one is assigned).
    # SET NULL so deleting a project doesn't cascade-delete its events.
    project_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    items: Mapped[list["Bug"]] = relationship(
        "Bug", back_populates="event",
        # Items are not deleted with the event; app code nulls the FK instead.
        passive_deletes=True,
    )
    managers: Mapped[list["User"]] = relationship(
        "User", secondary=event_managers, lazy="selectin",
    )
    # Eager-loaded so serialized responses can include project_name without N+1.
    project: Mapped["Project | None"] = relationship("Project")

    __table_args__ = (
        Index("idx_events_scheduled_for", "scheduled_for"),
        Index("idx_events_created_by", "created_by_user_id"),
        Index("idx_events_project_id", "project_id"),
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
    # Optional event link. SET NULL so deleting an event doesn't cascade
    # to the work items inside it.
    event_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_EVENTS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # The table is called "bugs" for historical compatibility, but each row
    # can be a Bug, a Requirement (story/spec), or a Task (standup item).
    # Default "Bug" so pre-existing rows without this column read correctly.
    item_type: Mapped[str] = mapped_column(String(20), nullable=False, default="Bug")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="New")
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="Medium")
    # Restricted to DEV / UAT / PROD (enforced in schemas).
    environment: Mapped[str] = mapped_column(String(10), nullable=False, default="DEV")
    due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    # Optimistic-concurrency counter, bumped on every update. updated_at alone
    # has whole-second resolution and misses same-second collisions; version
    # catches those. Starts at 1 for new and migrated rows.
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
        # Newest first; id DESC breaks ties within the same second.
        order_by="(Comment.created_at.desc(), Comment.id.desc())",
    )
    # Activity rows are not cascade-deleted with the bug. The audit trail must
    # outlive the items it describes: on bug delete the route nulls bug_id while
    # entity_id and the detail string keep the original bug id and title, so the
    # global audit screen still shows the full history.
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
        # Composite indexes for common dashboard queries; single-column indexes
        # above still handle single-field filters. create_all() is idempotent.
        Index("idx_bugs_project_status", "project_id", "status"),
        Index("idx_bugs_status_priority", "status", "priority"),
        Index("idx_bugs_updated_at", "updated_at"),
        # The default list page orders by (updated_at DESC, id DESC); the
        # composite lets the DB satisfy that ordering from the index directly.
        Index("idx_bugs_updated_id", "updated_at", "id"),
        # The stats timeline (GROUP BY date(created_at)) and oldest-first report
        # orderings scan on created_at. Additive; added by init_db on next boot.
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
# Files are stored as BLOBs inside the database, so no external object store
# is needed and a single DB backup covers all attachments. The trade-off is
# that large videos can bloat the DB; uploads are capped at 50 MB per file
# (config-driven). Moving to S3 later would only change the storage layer.
#
# An attachment belongs to a bug directly (bug_id set, comment_id NULL) or to
# a comment (both set; the bug FK lets bug-level queries still find it).
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
    # Deferred so listing attachments doesn't load the full BLOB. The download
    # endpoint accesses .data lazily, which issues a single SELECT for the bytes.
    # Python-side loading strategy only; the schema is unchanged.
    data: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    bug: Mapped[Bug] = relationship("Bug", back_populates="attachments", foreign_keys=[bug_id])

    __table_args__ = (
        Index("idx_attachments_bug_id", "bug_id"),
        Index("idx_attachments_comment_id", "comment_id"),
        # Composite for the per-bug "split attachments into bug-level vs
        # per-comment" query in routes/bugs.py.
        Index("idx_attachments_bug_comment", "bug_id", "comment_id"),
    )


# ---------------------------------------------------------------------------
# Activity (audit trail)
#
# bug_id is nullable so non-bug events (user created, project deleted, etc.)
# can be logged to the same table and appear on the global audit screen.
# ---------------------------------------------------------------------------
class Activity(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # SET NULL (not CASCADE) so audit rows survive when a bug is deleted.
    # entity_id and the detail string still reference the original bug, so
    # the audit screen can show the full history of a deleted item.
    bug_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    # entity_type + entity_id reference any object type (user, project, bug,
    # comment, attachment) without a FK — just metadata.
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
        # Reports filter action == "status_changed" joined to bugs (throughput,
        # time-to-resolution, timeline). Without this they scan the full audit
        # table. The audit "filter by actor" screen uses actor_user_id. Both
        # added additively by init_db().
        Index("idx_activity_action_bug", "action", "bug_id"),
        # The resolution/throughput/timeline reports order by (bug_id, created_at);
        # this index extension lets them avoid a sort over the audit table.
        Index("idx_activity_action_bug_created", "action", "bug_id", "created_at"),
        # The same reports also filter by action + created_at range without
        # constraining bug_id. The (action, bug_id, created_at) index above can't
        # seek that range (bug_id sits between the two predicates), so this
        # (action, created_at) index keeps the date-windowed scan sargable.
        Index("idx_activity_action_created", "action", "created_at"),
        Index("idx_activity_actor_user_id", "actor_user_id"),
    )


# ---------------------------------------------------------------------------
# Session
#
# Server-side record of every active login, required for the admin
# "list / revoke sessions" feature. Stateless signed cookies alone can't be
# selectively revoked because the server has no record of what it issued.
#
# Each row is keyed by a unique jti also embedded in the signed cookie.
# Every authenticated request looks up the row by jti; a missing or expired
# row rejects the cookie. The admin "revoke" action just deletes the row.
#
# Cookies issued before this table existed don't carry a jti. The auth layer
# accepts them, but they won't appear in the session list until next login.
# ---------------------------------------------------------------------------
class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    # Random opaque ID baked into the signed cookie; looked up on every request.
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Metadata for the admin session-list screen; not used for auth decisions.
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
# Per-user in-app notifications, parallel to the emails already sent by
# email_service. Recipients are determined the same way (reporter + assignees
# minus the actor, event managers, etc.), so a notification is only ever
# written for someone who is entitled to know. No endpoint ever returns another
# user's notifications.
#
# Scoped to user_id and cascade-deletes with the user.
# ---------------------------------------------------------------------------
class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    # Drives the icon/label on the frontend:
    # "assigned" | "reported" | "updated" | "comment" | "event".
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Deep-links for clicking through to the related item. CASCADE so a
    # notification is removed when its bug or event is deleted.
    bug_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=True
    )
    event_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_EVENTS_ID, ondelete="CASCADE"), nullable=True
    )
    # No FK, so the snapshot survives if the actor's account is deleted.
    actor_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # NULL means unread.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL until the daily digest job sends this notification; then stamped with
    # the send time so the job is idempotent. Independent of read_at: a row can
    # be read in-app but not yet digested, or vice-versa.
    emailed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_notifications_user_id", "user_id"),
        # The unread badge and unread list both filter on (user_id, read_at).
        Index("idx_notifications_user_read", "user_id", "read_at"),
        # The panel lists a user's notifications newest-first.
        Index("idx_notifications_user_created", "user_id", "created_at"),
        # The digest job scans for rows where emailed_at IS NULL.
        Index("idx_notifications_emailed_at", "emailed_at"),
        # It also applies a created_at cutoff; the composite lets the job seek
        # into the window instead of scanning all un-emailed rows. Additive.
        Index("idx_notifications_emailed_created", "emailed_at", "created_at"),
    )


# ---------------------------------------------------------------------------
# Push subscriptions (web push via Firebase Cloud Messaging).
#
# One row per device/browser that has granted push permission, keyed by the
# FCM registration token. The same table serves native Android clients
# (platform="android"). Push is sent immediately, not batched in the digest.
# ---------------------------------------------------------------------------
class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    # FCM registration token; unique per device/browser install.
    token: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    # "web" by default; "android"/"ios" when native clients register here.
    platform: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    # Coarse device hint for the user's device list and debugging.
    user_agent: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # Refreshed whenever the client re-subscribes (tokens rotate). A cleanup
    # job can use this to drop stale tokens.
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
# the cloud LLM can be given a rolling history. The in-process memory store
# still handles fast per-turn referents; these tables are the durable transcript.
#
# Both tables are scoped to user_id and cascade-delete with the user.
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
        # id is the tiebreaker because _utcnow() truncates to whole seconds;
        # without it a user+assistant turn in the same second could replay in
        # an unstable order. Consistent with Bug.comments/activities ordering.
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
    # Which engine produced the response: "rules" | "classifier" | "llm" | "cloud" | "".
    # Observability only; never used for auth.
    engine: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    conversation: Mapped[ChatConversation] = relationship(
        "ChatConversation", back_populates="messages"
    )

    __table_args__ = (Index("idx_chat_msg_conversation_id", "conversation_id"),)


# ---------------------------------------------------------------------------
# BugLink
#
# Directed relationship between two work items: "#12 blocks #34", "#5
# duplicates #2", etc. Stored as a single directed edge (source -> target)
# with a link_type; the route layer derives the inverse label ("is blocked by")
# so no redundant reverse rows are needed. Both FKs CASCADE so the link row
# is removed when either connected item is deleted.
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
    # "relates" | "blocks" | "duplicate"; validated in the schema layer.
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
        # One edge per (source, target, type); re-linking the same pair is a no-op.
        Index("idx_bug_links_unique", "source_bug_id", "target_bug_id", "link_type", unique=True),
        Index("idx_bug_links_source", "source_bug_id"),
        Index("idx_bug_links_target", "target_bug_id"),
    )
