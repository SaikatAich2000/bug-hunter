"""Pydantic schemas: projects."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import MIN_PROJECT_NAME_LENGTH, _strip_and_check_min_length


# --- Project ---
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


class ProjectCreateIn(ProjectIn):
    """Create payload. `agile_enabled` is honored only on POST so a freshly
    created project gets its default Sprint Board provisioned in the same
    transaction; PUT deliberately ignores the field (Agile is turned on/off
    through /api/agile/projects/{id}/enable|disable, which enforces the
    board/status provisioning and the active-sprint guard)."""
    agile_enabled: bool = False


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str
    color: str
    created_at: datetime
    updated_at: datetime
