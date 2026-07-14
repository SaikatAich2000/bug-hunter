"""ORM models for Bug Hunter (users, projects, bugs, comments, attachments,
audit log, reset tokens, sessions, notifications, push, chat, links)."""
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


# FK target strings as constants to avoid repeating the literal.
_FK_BUGS_ID = "bugs.id"
_FK_USERS_ID = "users.id"
_FK_PROJECTS_ID = "projects.id"
_FK_COMMENTS_ID = "comments.id"
_FK_EVENTS_ID = "events.id"

# Shared cascade/ondelete strings.
_CASCADE_ALL_DELETE_ORPHAN = "all, delete-orphan"
_ONDELETE_SET_NULL = "SET NULL"


# --- Junctions ---
bug_assignees = Table(
    "bug_assignees",
    Base.metadata,
    Column("bug_id", Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), primary_key=True),
)
# user_id is the trailing PK column; standalone index for "bugs assigned to X".
Index("idx_bug_assignees_user_id", bug_assignees.c.user_id)

# event_managers: events <-> owning users. Event-level notifications go here;
# tasks inside an event notify their own assignees separately.
event_managers = Table(
    "event_managers",
    Base.metadata,
    Column("event_id", Integer, ForeignKey(_FK_EVENTS_ID, ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), primary_key=True),
)
# user_id is the trailing PK column; standalone index for "events managed by X".
Index("idx_event_managers_user_id", event_managers.c.user_id)

# user_projects: project-scoped access. Non-admins see only items/events/stats/
# reports/audit for projects they belong to; a user with no rows sees nothing.
# Admins see everything regardless. Both FKs CASCADE.
user_projects = Table(
    "user_projects",
    Base.metadata,
    Column("user_id", Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), primary_key=True),
    Column("project_id", Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), primary_key=True),
)
# project_id is the trailing PK column; standalone index for "members of X".
Index("idx_user_projects_project_id", user_projects.c.project_id)


# Roles are enforced in app code, not DB constraints. Authoritative rules:
# app/auth.py (can_edit_bug / can_delete_bug).
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

    # bcrypt hash. Nullable to leave room for SSO, but normally always set.
    password_hash: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Bumped on password change/reset/forced logout; a cookie with an older
    # version is rejected, logging out other devices.
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    # Case-insensitive uniqueness enforced in app code (routes/users.py
    # `_email_in_use`); a DB expression index is skipped since SQLite can't
    # reflect one (breaks the additive-index idempotency check).
    __table_args__ = (Index("idx_users_email", "email"),)


# Single-use email password-reset tokens, stored as a sha256 hash never plaintext.
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
        # Covers the (user_id, used_at) filter in invalidate_outstanding_reset_tokens.
        Index("idx_prt_user_id_used_at", "user_id", "used_at"),
    )


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


# Groups work items (standup/sprint). Items link via bugs.event_id; deleting an
# event NULLs the FK rather than deleting items, preserving history.
class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # YYYY-MM-DD, consistent with Bug.due_date.
    scheduled_for: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Owning project; scopes visibility. Nullable (no backfill needed).
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
        # App code nulls the FK; items aren't deleted with the event.
        passive_deletes=True,
    )
    managers: Mapped[list["User"]] = relationship(
        "User", secondary=event_managers, lazy="selectin",
    )
    # Eager-loaded so responses include project_name without N+1.
    project: Mapped["Project | None"] = relationship("Project")

    __table_args__ = (
        Index("idx_events_scheduled_for", "scheduled_for"),
        Index("idx_events_created_by", "created_by_user_id"),
        Index("idx_events_project_id", "project_id"),
    )


# Bug = work item (Bug / Requirement / Task).
class Bug(Base):
    __tablename__ = "bugs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    reporter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    # Optional event link; SET NULL so deleting an event doesn't cascade to items.
    event_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_EVENTS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Bug / Requirement / Task; default "Bug" so pre-column rows read correctly.
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
    # Optimistic-concurrency counter, bumped on every update; catches same-second
    # collisions that updated_at's whole-second resolution misses.
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
        # Newest first; id DESC breaks same-second ties.
        order_by="(Comment.created_at.desc(), Comment.id.desc())",
    )
    # Audit rows outlive deleted bugs: on delete the route nulls bug_id while
    # entity_id/detail keep the original id and title (not cascade-deleted).
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
        # Composites for common dashboard queries.
        Index("idx_bugs_project_status", "project_id", "status"),
        Index("idx_bugs_status_priority", "status", "priority"),
        Index("idx_bugs_updated_at", "updated_at"),
        # Backs the default list ordering (updated_at DESC, id DESC).
        Index("idx_bugs_updated_id", "updated_at", "id"),
        # Backs the stats timeline and oldest-first report scans.
        Index("idx_bugs_created_at", "created_at"),
    )


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


# Files stored as BLOBs in the DB (uploads capped at 50 MB, config-driven).
# Belongs to a bug directly (comment_id NULL) or to a comment (both set; the
# bug FK keeps it findable by bug-level queries).
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
    # Deferred so listing doesn't load the BLOB; the download endpoint reads
    # .data lazily. Loading strategy only; schema unchanged.
    data: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    bug: Mapped[Bug] = relationship("Bug", back_populates="attachments", foreign_keys=[bug_id])

    __table_args__ = (
        Index("idx_attachments_bug_id", "bug_id"),
        Index("idx_attachments_comment_id", "comment_id"),
        # Backs the bug-level vs per-comment split query in routes/bugs.py.
        Index("idx_attachments_bug_comment", "bug_id", "comment_id"),
    )


# Audit trail. bug_id nullable so non-bug events (user/project/etc.) log here too.
class Activity(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # SET NULL so audit rows survive bug deletion; entity_id/detail keep the
    # original reference.
    bug_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    # entity_type + entity_id reference any object type without a FK; metadata only.
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
        # Reports filter action == "status_changed" joined to bugs.
        Index("idx_activity_action_bug", "action", "bug_id"),
        # Lets resolution/throughput/timeline reports avoid a sort over the audit table.
        Index("idx_activity_action_bug_created", "action", "bug_id", "created_at"),
        # Keeps the action + created_at range scan sargable (bug_id sits between
        # the predicates in the index above).
        Index("idx_activity_action_created", "action", "created_at"),
        Index("idx_activity_actor_user_id", "actor_user_id"),
    )


# Server-side login records for admin list/revoke. Keyed by a jti also embedded
# in the signed cookie; every request looks it up, a missing/expired row rejects
# the cookie, and revoke deletes the row. Pre-table cookies carry no jti and are
# accepted but absent from the list until next login.
class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    # Random opaque ID baked into the signed cookie; looked up on every request.
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Display metadata for the admin session list; not used for auth.
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


# Per-user in-app notifications, parallel to email_service. Same recipients
# (reporter + assignees minus actor, event managers). Scoped to user_id,
# cascade-deletes with the user; no endpoint returns another user's rows.
class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    # Drives the frontend icon/label:
    # "assigned" | "reported" | "updated" | "comment" | "event".
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Deep-link targets; CASCADE so the row is removed with its bug or event.
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
    # NULL until the daily digest sends it, then stamped (idempotent). Independent
    # of read_at.
    emailed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_notifications_user_id", "user_id"),
        # Unread badge/list filter on (user_id, read_at).
        Index("idx_notifications_user_read", "user_id", "read_at"),
        # Panel lists newest-first per user.
        Index("idx_notifications_user_created", "user_id", "created_at"),
        # Digest job scans emailed_at IS NULL.
        Index("idx_notifications_emailed_at", "emailed_at"),
        # Composite lets the digest seek into the created_at window.
        Index("idx_notifications_emailed_created", "emailed_at", "created_at"),
    )


# Web push (FCM): one row per device that granted permission, keyed by the FCM
# token. Also serves native clients (platform). Sent immediately, not digested.
class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    # FCM registration token; unique per device/browser install.
    token: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    # "web" default; "android"/"ios" for native clients.
    platform: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    # Coarse device hint for the device list and debugging.
    user_agent: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # Refreshed on re-subscribe (tokens rotate); a cleanup job can drop stale ones.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_push_subscriptions_user_id", "user_id"),
        Index("idx_push_subscriptions_token", "token"),
    )


# Sleuth chat memory: durable transcript surviving restarts, giving the LLM a
# rolling history. Both tables scoped to user_id, cascade-delete with the user.
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
        # id tiebreaker: _utcnow() truncates to whole seconds, so a same-second
        # turn would otherwise replay in an unstable order.
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
    # Engine that produced the response: "rules" | "classifier" | "llm" | "cloud" | "".
    # Observability only; never used for auth.
    engine: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    conversation: Mapped[ChatConversation] = relationship(
        "ChatConversation", back_populates="messages"
    )

    __table_args__ = (Index("idx_chat_msg_conversation_id", "conversation_id"),)


# Directed relationship between two items (e.g. "#12 blocks #34"). One edge
# (source -> target) with a link_type; the route derives the inverse label, so
# no reverse rows. Both FKs CASCADE.
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
        # One edge per (source, target, type); re-linking is a no-op.
        Index("idx_bug_links_unique", "source_bug_id", "target_bug_id", "link_type", unique=True),
        Index("idx_bug_links_source", "source_bug_id"),
        Index("idx_bug_links_target", "target_bug_id"),
    )
