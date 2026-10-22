"""Pydantic schemas: events."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.bugs import BugOut
from app.schemas.common import _reject_explicit_nulls, _strip_and_check_min_length
from app.schemas.users import UserBrief


# Events — container for a group of work items (standup / sprint meeting).
class EventCreate(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=10000)
    scheduled_for: str | None = None  # YYYY-MM-DD
    # Owning project, scopes visibility. Optional (project-less = admins only);
    # route validates existence and the creator's access.
    project_id: int | None = None
    # Admin/manager users to notify on event create/update/delete (not per-task).
    manager_ids: list[int] = Field(default_factory=list, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 2, "Event name")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    @field_validator("scheduled_for")
    @classmethod
    def _check_date(cls, v: str | None) -> str | None:
        if v in (None, ""): return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("scheduled_for must be YYYY-MM-DD") from exc
        return v

    @field_validator("manager_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen: seen.append(x)
        return seen


class EventUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    scheduled_for: str | None = None
    manager_ids: list[int] | None = Field(default=None, max_length=200)
    # Move to a different project; None/omit = unchanged. Route validates access.
    project_id: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_nulls(cls, data):
        return _reject_explicit_nulls(data, ("name",))

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None: return None
        return _strip_and_check_min_length(v, 2, "Event name")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v

    @field_validator("scheduled_for")
    @classmethod
    def _check_date(cls, v: str | None) -> str | None:
        if v in (None, ""): return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("scheduled_for must be YYYY-MM-DD") from exc
        return v

    @field_validator("manager_ids")
    @classmethod
    def _dedup(cls, v: list[int] | None) -> list[int] | None:
        if v is None: return None
        seen: list[int] = []
        for x in v:
            if x not in seen: seen.append(x)
        return seen


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str
    scheduled_for: str | None = None
    # Nullable for pre-column events; project_name resolved by the route.
    project_id: int | None = None
    project_name: str | None = None
    created_by_user_id: int | None = None
    created_by_name: str | None = None
    item_count: int = 0
    assignee_count: int = 0
    # Full briefs so the UI renders names/emails without an extra round-trip.
    managers: list[UserBrief] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class EventDetail(EventOut):
    """Event with its full item list, returned by /api/events/{id}."""
    items: list[BugOut] = Field(default_factory=list)
    # True when the item list hit the server ceiling (client shows "N of M").
    items_truncated: bool = False
