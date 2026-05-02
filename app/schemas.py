"""Pydantic schemas (request/response DTOs)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


ALLOWED_STATUSES = ["New", "In Progress", "Resolved", "Closed", "Reopened"]
ALLOWED_PRIORITIES = ["Low", "Medium", "High", "Critical"]
ALLOWED_ENVIRONMENTS = ["DEV", "UAT", "PROD"]
ALLOWED_ROLES = ["admin", "manager", "user"]
MIN_PASSWORD_LENGTH = 8
MIN_TITLE_LENGTH = 3
MIN_NAME_LENGTH = 2
MIN_PROJECT_NAME_LENGTH = 2


def normalize_choice(value: str, allowed: list[str], label: str) -> str:
    """Case-insensitive match against `allowed`; returns canonical form.
    Public helper — also called from filter routes so list-filter and
    create-payload accept the same casings."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid {label}. Allowed: {', '.join(allowed)}")
    needle = value.strip().lower()
    for canonical in allowed:
        if canonical.lower() == needle:
            return canonical
    raise ValueError(f"Invalid {label}. Allowed: {', '.join(allowed)}")


# Kept as private alias for backward-compat inside this module.
_normalize_choice = normalize_choice


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _validate_email(value: str) -> str:
    v = (value or "").strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("Invalid email address")
    return v


def _strip_and_check_min_length(v: str, min_len: int, label: str) -> str:
    """Strip whitespace, then enforce min length. Without the post-strip
    check, '  a  ' would slip past Field(min_length=...) which only sees
    the raw, padded value."""
    if not isinstance(v, str):
        raise ValueError(f"{label} must be a string")
    v = v.strip()
    if len(v) < min_len:
        if min_len == 1:
            raise ValueError(f"{label} cannot be empty")
        raise ValueError(f"{label} must be at least {min_len} characters")
    return v


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------
def _normalize_role(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("role must be a string")
    needle = v.strip().lower()
    if needle in ALLOWED_ROLES:
        return needle
    raise ValueError(f"Invalid role. Allowed: {', '.join(ALLOWED_ROLES)}")


def _check_password_strength(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("Password must be a string")
    if len(v) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(v) > 200:
        raise ValueError("Password is too long")
    return v


class UserIn(BaseModel):
    """Admin creates a user (with a password)."""
    name: str = Field(max_length=120)
    email: str = Field(max_length=254)
    role: str = Field(default="user")
    password: str
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        return _normalize_role(v)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _check_password_strength(v)


class UserUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    email: Optional[str] = Field(default=None, max_length=254)
    role: Optional[str] = None
    is_active: Optional[bool] = None
    # Optional password reset by admin. If present, replaces the current
    # hash. Use None / omit to leave password unchanged.
    password: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _normalize_role(v)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _validate_email(v)

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _check_password_strength(v)


class UserOut(BaseModel):
    """Public, password never serialized."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)


class ChangePasswordIn(BaseModel):
    # Allow any non-empty current password — the server will bcrypt-verify
    # it. Length must match what we ever issued, but we can't introspect
    # historical hashes, so just guard against trivially-empty input.
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _check_password_strength(v)


class ForgotPasswordIn(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)


class ResetPasswordIn(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _check_password_strength(v)


class MeOut(BaseModel):
    """Returned to the frontend after login or on refresh."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str
    is_active: bool


class UserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------
class ProjectIn(BaseModel):
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=1000)
    color: str = Field(default="#c9764f", pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_PROJECT_NAME_LENGTH, "Project name")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str
    color: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Bug
# ---------------------------------------------------------------------------
class BugCreate(BaseModel):
    project_id: int
    title: str = Field(max_length=200)
    description: str = Field(default="", max_length=10000)
    reporter_id: Optional[int] = None
    assignee_ids: list[int] = Field(default_factory=list)
    status: str = Field(default="New")
    priority: str = Field(default="Medium")
    environment: str = Field(default="DEV")
    due_date: Optional[str] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        return _normalize_choice(v, ALLOWED_STATUSES, "status")

    @field_validator("priority")
    @classmethod
    def _check_priority(cls, v: str) -> str:
        return _normalize_choice(v, ALLOWED_PRIORITIES, "priority")

    @field_validator("environment")
    @classmethod
    def _check_env(cls, v: str) -> str:
        return _normalize_choice(v, ALLOWED_ENVIRONMENTS, "environment")

    @field_validator("due_date")
    @classmethod
    def _check_due(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""): return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("due_date must be YYYY-MM-DD") from exc
        return v

    @field_validator("assignee_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen: seen.append(x)
        return seen


class BugUpdate(BaseModel):
    project_id: Optional[int] = None
    title: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=10000)
    reporter_id: Optional[int] = None
    assignee_ids: Optional[list[int]] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    environment: Optional[str] = None
    due_date: Optional[str] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _normalize_choice(v, ALLOWED_STATUSES, "status")

    @field_validator("priority")
    @classmethod
    def _check_priority(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _normalize_choice(v, ALLOWED_PRIORITIES, "priority")

    @field_validator("environment")
    @classmethod
    def _check_env(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _normalize_choice(v, ALLOWED_ENVIRONMENTS, "environment")

    @field_validator("due_date")
    @classmethod
    def _check_due(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""): return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("due_date must be YYYY-MM-DD") from exc
        return v

    @field_validator("assignee_ids")
    @classmethod
    def _dedup(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is None: return None
        seen: list[int] = []
        for x in v:
            if x not in seen: seen.append(x)
        return seen


class AttachmentBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    content_type: str
    size_bytes: int
    uploader_user_id: Optional[int] = None
    uploader_name: str
    comment_id: Optional[int] = None
    created_at: datetime


class BugOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    project_name: Optional[str] = None
    title: str
    description: str
    reporter: Optional[UserBrief] = None
    assignees: list[UserBrief] = Field(default_factory=list)
    status: str
    priority: str
    environment: str
    due_date: Optional[str]
    created_at: datetime
    updated_at: datetime
    attachment_count: int = 0
    can_edit: bool = False


class BugListResponse(BaseModel):
    items: list[BugOut]
    page: int
    page_size: int
    total: int
    pages: int


# ---------------------------------------------------------------------------
# Comment / Activity / Detail
# ---------------------------------------------------------------------------
class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)

    @field_validator("body")
    @classmethod
    def _strip(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 1, "Comment body")


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    bug_id: int
    author_user_id: Optional[int] = None
    author_name: str
    body: str
    created_at: datetime
    attachments: list[AttachmentBrief] = Field(default_factory=list)


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    bug_id: Optional[int] = None
    entity_type: str
    entity_id: Optional[int] = None
    actor_user_id: Optional[int] = None
    actor_name: str
    action: str
    detail: str
    created_at: datetime


class BugDetail(BugOut):
    comments: list[CommentOut] = Field(default_factory=list)
    activities: list[ActivityOut] = Field(default_factory=list)
    attachments: list[AttachmentBrief] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
class StatsOut(BaseModel):
    projects: int
    bugs: int
    users: int
    open: int
    resolved: int
    by_status: dict[str, int]
    by_priority: dict[str, int]
    by_environment: dict[str, int]
    by_project: list[dict[str, Any]]
    by_assignee: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
