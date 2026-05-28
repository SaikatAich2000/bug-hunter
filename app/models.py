"""ORM models for Bug Hunter.

Tables:
  - users            : team members
  - projects         : workspaces
  - bugs             : core entity (slimmer than v2.1: no severity, labels,
                       steps_to_reproduce, expected_result, actual_result)
  - bug_assignees    : many-to-many between bugs and users
  - comments         : threaded discussion on a bug
  - attachments      : file blobs (PDF / image / video) attached to a bug
                       OR to a comment. Stored INSIDE the database so they
                       persist across restarts and survive backups.
  - activity_log     : audit trail
  - password_reset_tokens : single-use email-based password reset tokens
  - sessions         : server-side record of every active login. Lets
                       admins see who's currently signed in (Keycloak-style)
                       and revoke individual sessions to forcibly log a
                       specific device out without affecting any others.
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


# ---------------------------------------------------------------------------
# Junctions
# ---------------------------------------------------------------------------
bug_assignees = Table(
    "bug_assignees",
    Base.metadata,
    Column("bug_id", Integer, ForeignKey("bugs.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)

# event_managers: many-to-many between events and the (admin/manager) users
# who own that event. Notifications about the event (create / edit /
# delete) fan out to this list. Tasks created INSIDE the event email
# their own assignees as normal — they do NOT cc the event managers, so
# adding someone here doesn't sign them up for every task email.
event_managers = Table(
    "event_managers",
    Base.metadata,
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


# ---------------------------------------------------------------------------
# User
#
# Roles (enforced in app code, not DB constraints, for flexibility):
#   admin    - full access; only admins manage users
#   manager  - can edit any bug or project, but not users
#   user     - default; can only edit bugs they reported or are assigned to
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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

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
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (Index("idx_prt_token_hash", "token_hash"),)


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
        "Bug", back_populates="project", cascade="all, delete-orphan"
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
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
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
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    reporter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Optional link to an event (standup / sprint meeting). Nullable so a
    # work item can exist independently. Delete behaviour: when the event
    # row is deleted, the FK is NULLed via SQL (SET NULL), preserving the
    # work item itself.
    event_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True
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

    project: Mapped[Project] = relationship("Project", back_populates="bugs")
    reporter: Mapped["User | None"] = relationship("User", foreign_keys=[reporter_id])
    event: Mapped["Event | None"] = relationship("Event", back_populates="items")
    assignees: Mapped[list["User"]] = relationship(
        "User", secondary=bug_assignees, lazy="selectin"
    )
    comments: Mapped[list["Comment"]] = relationship(
        "Comment", back_populates="bug", cascade="all, delete-orphan",
        order_by="Comment.created_at",
    )
    # Activities ordered newest-first with an id-DESC tiebreaker so two
    # events recorded in the same second still come back in a stable,
    # insert-consistent order. Without the id tiebreaker the relationship
    # ordering disagreed with the dedicated /activity endpoint, which had
    # this same tuple already.
    # Note: we deliberately do NOT cascade-delete activity rows when a bug
    # is deleted. The audit trail must outlive the bugs it describes so an
    # admin can still find "who deleted bug #42 and when". On delete the
    # route handler detaches the rows (bug_id → NULL); the entity_id stays
    # set to the original bug id and the detail string preserves the title,
    # so the global audit screen still shows the full history.
    activities: Mapped[list["Activity"]] = relationship(
        "Activity", back_populates="bug",
        order_by="(Activity.created_at.desc(), Activity.id.desc())",
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        "Attachment", back_populates="bug", cascade="all, delete-orphan",
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
        # v3.2 additive composite indexes — speed up the common dashboard
        # queries which filter on multiple columns at once. Single-column
        # indexes above still serve queries that filter on just one field.
        # SQLAlchemy create_all() is idempotent, so adding these on a live
        # DB is safe — existing data and indexes are untouched.
        Index("idx_bugs_project_status", "project_id", "status"),
        Index("idx_bugs_status_priority", "status", "priority"),
        Index("idx_bugs_updated_at", "updated_at"),
    )


# ---------------------------------------------------------------------------
# Comment
# ---------------------------------------------------------------------------
class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(Integer, ForeignKey("bugs.id", ondelete="CASCADE"), nullable=False)
    author_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
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
#   - One backup of the database = full backup of all attachments.
#   - Survives container restart, host migration, anything.
#
# Trade-off: very large videos can bloat the DB. We cap upload size at
# 50 MB per file (config-driven) so this stays reasonable for an
# internal tool. If you ever outgrow this, swap data->S3 with no API
# changes — only the storage layer.
#
# An attachment can belong to a bug directly (bug_id set, comment_id NULL)
# or to a comment (both set; comment FK lives in addition to bug FK so a
# bug-level query still finds it).
# ---------------------------------------------------------------------------
class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bugs.id", ondelete="CASCADE"), nullable=False
    )
    comment_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("comments.id", ondelete="CASCADE"), nullable=True
    )
    uploader_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    uploader_name: Mapped[str] = mapped_column(String(120), nullable=False, default="anonymous")
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Deferred (v3.2.1) — the BLOB is only fetched when actually read
    # (.data is touched). Listing attachments for a bug or comment no
    # longer pulls the full file content from the DB; the download
    # endpoint still works because accessing `a.data` lazily issues a
    # single SELECT for the bytes. Schema unchanged — this is a Python-
    # side loading strategy, safe for the live DB.
    data: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    bug: Mapped[Bug] = relationship("Bug", back_populates="attachments", foreign_keys=[bug_id])

    __table_args__ = (
        Index("idx_attachments_bug_id", "bug_id"),
        Index("idx_attachments_comment_id", "comment_id"),
        # v3.2: composite supports the per-bug "load all attachments and
        # split into bug-level vs by-comment" pattern in routes/bugs.py.
        Index("idx_attachments_bug_comment", "bug_id", "comment_id"),
    )


# ---------------------------------------------------------------------------
# Activity (audit trail)
#
# Same purpose as before — but `bug_id` is now nullable so we can also
# log non-bug events (user created, project deleted, etc.) for the
# global audit-trail screen.
# ---------------------------------------------------------------------------
class Activity(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # bug_id is SET NULL on delete (not CASCADE) so audit history survives
    # when a bug is deleted. The row keeps its entity_type / entity_id
    # pointing at the (now gone) bug, and the detail string preserves the
    # title — so searching the audit trail for that bug still works.
    bug_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bugs.id", ondelete="SET NULL"), nullable=True
    )
    # entity_type + entity_id let us reference any object: "user", "project",
    # "bug", "comment", "attachment". Lightweight — no FK, just metadata.
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, default="bug")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
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
    )


# ---------------------------------------------------------------------------
# Session
#
# Server-side record of every active login. We need this for the Keycloak-
# style "list / revoke active sessions" admin feature: stateless signed
# cookies alone can't be selectively revoked because the server keeps no
# record of which tokens it has issued.
#
# Each session row is keyed by a unique `jti` (JWT-style ID) which is also
# baked into the signed session cookie. On every authenticated request we
# look up the row by jti — if it's missing or expired, the cookie is
# rejected. The admin "revoke" action just deletes the row.
#
# Backward compatibility note: tokens issued before this table existed
# don't carry a jti. The auth layer accepts those legacy tokens but they
# don't appear in the session list (they will the next time the user logs
# in fresh).
# ---------------------------------------------------------------------------
class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Random opaque ID baked into the signed cookie. We look up sessions
    # by this on every authenticated request.
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Best-effort metadata for the admin session-list screen. Never used
    # for auth decisions — purely informational.
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
