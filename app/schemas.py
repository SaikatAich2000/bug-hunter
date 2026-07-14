"""Pydantic request/response schemas."""
from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# Server-side HTML sanitizer for rich-text fields (description, comment body):
# the contenteditable editor emits HTML and storing it raw would be stored XSS.
# Hand-rolled allowlist (avoids bleach/html5lib); swap for bleach if tags grow.
_ALLOWED_TAGS = {
    "p", "br", "div", "span",
    "b", "strong", "i", "em", "u", "s", "strike", "del", "ins",
    "ul", "ol", "li",
    "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "img",
}
_ALLOWED_ATTRS = {
    # Anything not listed is stripped even on allowed tags (blocks <img onerror=>).
    # `rel` on <a> is omitted so our forced rel="noopener nofollow" can't be stripped.
    "a":   {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "code": {"class"},   # editor may emit `<code class="language-X">`
    "pre":  {"class"},
}
# Allowed URL schemes for href/src; data: is handled separately below.
_ALLOWED_URL_SCHEMES = ("http:", "https:", "mailto:", "/", "#")
# Raster data: URLs only — data:image/svg+xml is scriptable, so it's excluded.
_DATA_IMAGE_RASTER_PREFIXES = (
    "data:image/png", "data:image/jpeg", "data:image/jpg",
    "data:image/gif", "data:image/webp", "data:image/bmp", "data:image/avif",
)
# RCDATA/CDATA elements: their text content is dropped too (not just the tag),
# closing a parser-differential / mutation-XSS risk against a real HTML5 tokenizer.
_RCDATA_DROP_TAGS = frozenset({
    "script", "style", "textarea", "title", "noscript", "xmp",
    "iframe", "noframes", "template",
})


class _HTMLAllowlistSanitizer(HTMLParser):
    """Drops every tag/attr not on the allowlist; text content always survives."""
    # convert_charrefs=False keeps &amp;/&lt; as-is instead of collapsing them.
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        # Nesting depth inside RCDATA elements, so their text is suppressed.
        self._drop_text_depth = 0

    def _safe_url(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        s = raw.strip()
        low = s.lower()
        # Inline pasted screenshots only (uploads take a different path).
        if low.startswith("data:image/"):
            # Raster bitmaps only (SVG is scriptable); size-capped to bound storage.
            if not low.startswith(_DATA_IMAGE_RASTER_PREFIXES):
                return None
            if len(s) > 14 * 1024 * 1024:
                return None
            return s
        for scheme in _ALLOWED_URL_SCHEMES:
            if low.startswith(scheme):
                return s
        return None

    def _kept_attrs(self, t: str, attrs: list[tuple[str, Optional[str]]]) -> list[str]:
        """Return the allowed name="value" attribute strings for tag t."""
        kept: list[str] = []
        allowed_attrs = _ALLOWED_ATTRS.get(t, set())
        for k, v in attrs:
            k = k.lower()
            if k not in allowed_attrs or v is None:
                continue
            if k in ("href", "src"):
                clean = self._safe_url(v)
                if not clean:
                    continue
                v = clean
            # Escape for HTML embedding.
            v_safe = (v.replace("&", "&amp;").replace("<", "&lt;")
                       .replace(">", "&gt;").replace('"', "&quot;"))
            kept.append(f'{k}="{v_safe}"')
        # Force our own rel on <a> so reverse-tabnabbing hardening can't be stripped.
        if t == "a":
            kept.append('rel="noopener nofollow"')
        return kept

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in _RCDATA_DROP_TAGS:
            self._drop_text_depth += 1
            return
        if t not in _ALLOWED_TAGS:
            return
        kept = self._kept_attrs(t, attrs)
        attr_str = (" " + " ".join(kept)) if kept else ""
        self.out.append(f"<{t}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _RCDATA_DROP_TAGS:
            if self._drop_text_depth > 0:
                self._drop_text_depth -= 1
            return
        if t not in _ALLOWED_TAGS:
            return
        # Void elements have no closing tag.
        if t in ("br", "img"):
            return
        self.out.append(f"</{t}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # <br/> / <img .../> re-emit as a start tag. A self-closing RCDATA tag
        # is skipped without touching _drop_text_depth (it opens+closes at once).
        if tag.lower() in _RCDATA_DROP_TAGS:
            return
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._drop_text_depth > 0:
            return
        self.out.append(
            data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")


def sanitize_html(value: Optional[str]) -> str:
    """Return allowlist-sanitized HTML for storage/display; idempotent, "" for None."""
    if value is None:
        return ""
    s = str(value)
    p = _HTMLAllowlistSanitizer()
    p.feed(s)
    p.close()
    return "".join(p.out)


# Per-item-type status sets; "New" is in every list so the create default is
# always valid. Out-of-set statuses display as-is on read but are rejected on
# create/update. ALLOWED_STATUSES is the union for type-agnostic filters.
STATUSES_BY_TYPE = {
    "Bug": [
        "New", "In Progress", "Resolved", "Closed", "Reopened",
        "Not a Bug", "Resolve Later",
    ],
    "Requirement": [
        "New", "In Review", "Approved", "Implemented", "Rejected", "Deferred",
    ],
    "Task": [
        "New", "In Progress", "Done", "Blocked", "Cancelled",
    ],
}
ALLOWED_STATUSES = list(
    dict.fromkeys(s for sts in STATUSES_BY_TYPE.values() for s in sts)
)
# "Not a Bug" items are excluded from the "Total bugs" KPI on the dashboard.
EXCLUDED_FROM_TOTAL_STATUSES = ["Not a Bug"]


def statuses_for_type(item_type: str) -> list[str]:
    """Valid statuses for item_type; falls back to Bug for unknown/legacy types."""
    return STATUSES_BY_TYPE.get(item_type or "Bug", STATUSES_BY_TYPE["Bug"])
ALLOWED_PRIORITIES = ["Low", "Medium", "High", "Critical"]
ALLOWED_ENVIRONMENTS = ["DEV", "UAT", "PROD"]
# Work-item flavors; a classifier for filtering/badges, other fields apply to all.
ALLOWED_ITEM_TYPES = ["Bug", "Requirement", "Task"]
ALLOWED_ROLES = ["admin", "manager", "user"]
# Link kinds on the directed source→target edge; route renders the inverse label.
ALLOWED_LINK_TYPES = ["relates", "blocks", "duplicate"]
# Bulk toolbar actions; each reuses the single-item permission/audit/notify path.
ALLOWED_BULK_ACTIONS = [
    "set_status", "set_priority", "set_environment", "delete",
]
MIN_PASSWORD_LENGTH = 8
MIN_TITLE_LENGTH = 3
MIN_NAME_LENGTH = 2
MIN_PROJECT_NAME_LENGTH = 2


def normalize_choice(value: str, allowed: list[str], label: str) -> str:
    """Case-insensitive lookup against `allowed`; returns the canonical form."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid {label}. Allowed: {', '.join(allowed)}")
    needle = value.strip().lower()
    for canonical in allowed:
        if canonical.lower() == needle:
            return canonical
    raise ValueError(f"Invalid {label}. Allowed: {', '.join(allowed)}")


# Private alias kept for callers within this module.
_normalize_choice = normalize_choice


# Validates local part, domain labels, and an alphabetic TLD; quantifiers
# bounded to avoid ReDoS.
_EMAIL_RE = re.compile(
    r"^(?![.])(?!.*[.]{2})[A-Za-z0-9._%+\-]+(?<![.])@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$"
)


def _validate_email(value: str) -> str:
    v = (value or "").strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("Invalid email address")
    return v


def _strip_and_check_min_length(v: str, min_len: int, label: str) -> str:
    """Strip whitespace then enforce min length (Field(min_length) measures pre-strip)."""
    if not isinstance(v, str):
        raise ValueError(f"{label} must be a string")
    v = v.strip()
    if len(v) < min_len:
        if min_len == 1:
            raise ValueError(f"{label} cannot be empty")
        raise ValueError(f"{label} must be at least {min_len} characters")
    return v


# --- User ---
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
    # 'changeme' is the factory default; always accept so upgrades raising
    # PASSWORD_MIN_LENGTH don't lock out existing accounts.
    if v.lower() == "changeme":
        return v
    # DoS guard — bcrypt cost scales with input length.
    if len(v) > 200:
        raise ValueError("Password is too long")
    from app.config import get_settings  # local import avoids an import cycle
    settings = get_settings()
    min_len = max(1, settings.PASSWORD_MIN_LENGTH)
    if len(v) < min_len:
        raise ValueError(f"Password must be at least {min_len} characters")
    # No special-character mandate (NIST 800-63B §5.1.1.2); length + letter + digit.
    if settings.PASSWORD_REQUIRE_COMPLEXITY:
        has_letter = any(c.isalpha() for c in v)
        has_digit = any(c.isdigit() for c in v)
        if not (has_letter and has_digit):
            raise ValueError("Password must contain at least one letter and one number")
    # Reject a short list of universally-weak passwords by exact match.
    if v.lower() in {"password", "password1", "password123", "admin123",
                     "qwerty123", "12345678a", "letmein123"}:
        raise ValueError("Password is too common — please choose a stronger one")
    return v


class UserIn(BaseModel):
    """Admin creates a user."""
    name: str = Field(max_length=120)
    email: str = Field(max_length=254)
    role: str = Field(default="user")
    password: str
    is_active: bool = True
    # Projects a manager/user can access (admins ignore tags); untagged = sees
    # nothing. Route validates existence and the creator's access.
    project_ids: list[int] = Field(default_factory=list, max_length=1000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("project_ids")
    @classmethod
    def _dedup_projects(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen:
                seen.append(x)
        return seen

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
    # Admin password reset; replaces the hash if present, omit to leave unchanged.
    password: Optional[str] = None
    # Replace memberships. None/omit = unchanged; empty list = untag.
    project_ids: Optional[list[int]] = Field(default=None, max_length=1000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("project_ids")
    @classmethod
    def _dedup_projects(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is None: return None
        seen: list[int] = []
        for x in v:
            if x not in seen:
                seen.append(x)
        return seen

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
    """Public user view. Password is never serialized."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    # Project memberships (sorted by id), route-populated; [] = untagged.
    project_ids: list[int] = Field(default_factory=list)


# --- Auth ---
class LoginIn(BaseModel):
    # Bounded so an unauthenticated caller can't burn bcrypt CPU on a huge body.
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)


class ChangePasswordIn(BaseModel):
    # Accept any non-empty current password; bcrypt-verify decides.
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _check_password_strength(v)


class ForgotPasswordIn(BaseModel):
    # Length cap mirrors LoginIn (unauthenticated DoS risk).
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)


class ResetPasswordIn(BaseModel):
    # Token is a hex SHA-256 value; capped to bound hashing/comparison.
    token: str = Field(min_length=1, max_length=512)
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


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str
    color: str
    created_at: datetime
    updated_at: datetime


# --- Bug ---
class BugCreate(BaseModel):
    project_id: int
    title: str = Field(max_length=200)
    # Rich HTML; 1 MB ceiling fits inline pasted screenshots. Sanitized below.
    description: str = Field(default="", max_length=1_000_000)
    reporter_id: Optional[int] = None
    assignee_ids: list[int] = Field(default_factory=list, max_length=200)
    item_type: str = Field(default="Bug")
    status: str = Field(default="New")
    priority: str = Field(default="Medium")
    environment: str = Field(default="DEV")
    due_date: Optional[str] = None
    # Link to an event at creation time.
    event_id: Optional[int] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        # Sanitize SPA-editor HTML; strip outer whitespace so "<p><br></p>"
        # compares cleanly against empty-string checks.
        if not isinstance(v, str): return v
        return sanitize_html(v.strip())

    @field_validator("item_type")
    @classmethod
    def _check_item_type(cls, v: str) -> str:
        return _normalize_choice(v, ALLOWED_ITEM_TYPES, "item_type")

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        # Global union here; per-type check runs in _check_status_for_type below.
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

    @model_validator(mode="after")
    def _check_status_for_type(self) -> "BugCreate":
        # Cross-field: status must be valid for the chosen item_type.
        allowed = statuses_for_type(self.item_type)
        if self.status not in allowed:
            raise ValueError(
                f"Status '{self.status}' is not valid for {self.item_type}. "
                f"Allowed: {', '.join(allowed)}"
            )
        return self


class BugUpdate(BaseModel):
    project_id: Optional[int] = None
    title: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1_000_000)
    reporter_id: Optional[int] = None
    assignee_ids: Optional[list[int]] = Field(default=None, max_length=200)
    item_type: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    environment: Optional[str] = None
    due_date: Optional[str] = None
    # int to link, null/0 to unlink; route uses exclude_unset to tell "clear"
    # from "not provided".
    event_id: Optional[int] = None

    # Opt-in optimistic concurrency: echo the last-seen updated_at; a mismatch
    # returns 409. Omit for last-write-wins.
    expected_updated_at: Optional[str] = Field(default=None, max_length=64)

    # Preferred concurrency token: the integer version counter (sub-second, no
    # clock skew). Omit both fields for last-write-wins.
    expected_version: Optional[int] = Field(default=None, ge=0)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: Optional[str]) -> Optional[str]:
        # Same sanitization as BugCreate.description.
        if v is None: return None
        if not isinstance(v, str): return v
        return sanitize_html(v.strip())

    @field_validator("item_type")
    @classmethod
    def _check_item_type(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _normalize_choice(v, ALLOWED_ITEM_TYPES, "item_type")

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
    item_type: str = "Bug"
    status: str
    priority: str
    environment: str
    due_date: Optional[str] = None
    # Both nullable so standalone items (no event) keep the same shape.
    event_id: Optional[int] = None
    event_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    version: int = 1
    attachment_count: int = 0
    can_edit: bool = False


class BugListResponse(BaseModel):
    items: list[BugOut]
    page: int
    page_size: int
    total: int
    pages: int


# --- Comment / Activity / Detail ---
class CommentIn(BaseModel):
    # 200 KB ceiling fits a pasted screenshot; sanitizer strips dangerous payloads.
    body: str = Field(min_length=1, max_length=200_000)

    @field_validator("body")
    @classmethod
    def _strip(cls, v: str) -> str:
        # Sanitize server-side, then require visible text or an <img src> —
        # an all-tags/whitespace body is treated as empty.
        if not isinstance(v, str):
            raise ValueError("Comment body must be a string")
        cleaned = sanitize_html(v.strip())
        text_only = re.sub(r"<[^>]+>", "", cleaned).strip()
        # Check src explicitly so a src-less <img> doesn't count as non-empty.
        has_image = re.search(r"<img\b[^>]*\bsrc=", cleaned, re.IGNORECASE) is not None
        if not text_only and not has_image:
            raise ValueError("Comment body cannot be empty")
        return cleaned


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


# --- Item linking ---
class BugLinkIn(BaseModel):
    target_bug_id: int
    link_type: str = "relates"

    @field_validator("link_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        return _normalize_choice(v, ALLOWED_LINK_TYPES, "link_type")


class BugLinkOut(BaseModel):
    """One link from a bug's perspective. `direction` is outgoing (this bug is
    source) or incoming; `label` is the phrasing from this side."""
    id: int
    link_type: str
    direction: str          # "outgoing" | "incoming"
    label: str
    other_bug_id: int
    other_bug_title: str
    other_bug_status: str
    other_bug_item_type: str
    created_at: datetime


# Bulk actions — the multi-select toolbar on the list view.
class BulkActionIn(BaseModel):
    action: str
    ids: list[int] = Field(min_length=1, max_length=500)
    # Value for set_status / set_priority / set_environment; route normalizes it.
    value: Optional[str] = Field(default=None, max_length=20)
    # Optional {bug_id: version} map; drifted rows are reported as conflicts.
    expected_versions: Optional[dict[int, int]] = Field(default=None, max_length=500)

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        if v not in ALLOWED_BULK_ACTIONS:
            raise ValueError(f"Invalid action. Allowed: {', '.join(ALLOWED_BULK_ACTIONS)}")
        return v

    @field_validator("ids")
    @classmethod
    def _dedup_ids(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen:
                seen.append(x)
        return seen


class BulkActionResult(BaseModel):
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    # Rows skipped because their version drifted from expected_versions.
    conflicts: int = 0
    message: str = ""


class BugDetail(BugOut):
    comments: list[CommentOut] = Field(default_factory=list)
    activities: list[ActivityOut] = Field(default_factory=list)
    attachments: list[AttachmentBrief] = Field(default_factory=list)
    # Links in both directions, rendered from this item's perspective.
    links: list[BugLinkOut] = Field(default_factory=list)


# --- Sessions (admin only) ---
class SessionOut(BaseModel):
    """One row in the admin "active sessions" panel; `is_current` marks the
    admin's own session (self-revocation is rejected in routes/sessions.py)."""
    id: int
    user_id: int
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    user_role: Optional[str] = None
    ip_address: str
    user_agent: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    is_current: bool = False


# --- Stats ---
class StatsOut(BaseModel):
    # Total excluding "Not a Bug".
    bugs: int
    # KPI strip buckets on the dashboard.
    open: int
    resolved: int
    closed: int
    resolve_later: int
    # Kept for backward compatibility; the UI no longer renders these.
    projects: int = 0
    users: int = 0
    by_status: dict[str, int]
    by_priority: dict[str, int]
    by_environment: dict[str, int]
    by_type: dict[str, int] = Field(default_factory=dict)
    by_project: list[dict[str, Any]]
    by_assignee: list[dict[str, Any]]
    timeline: list[dict[str, Any]]


# --- Events — container for a group of work items (standup / sprint meeting). ---
class EventCreate(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=10000)
    scheduled_for: Optional[str] = None  # YYYY-MM-DD
    # Owning project, scopes visibility. Optional (project-less = admins only);
    # route validates existence and the creator's access.
    project_id: Optional[int] = None
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
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
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
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=10000)
    scheduled_for: Optional[str] = None
    manager_ids: Optional[list[int]] = Field(default=None, max_length=200)
    # Move to a different project; None/omit = unchanged. Route validates access.
    project_id: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _strip_and_check_min_length(v, 2, "Event name")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v

    @field_validator("scheduled_for")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""): return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("scheduled_for must be YYYY-MM-DD") from exc
        return v

    @field_validator("manager_ids")
    @classmethod
    def _dedup(cls, v: Optional[list[int]]) -> Optional[list[int]]:
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
    scheduled_for: Optional[str] = None
    # Nullable for pre-column events; project_name resolved by the route.
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    created_by_user_id: Optional[int] = None
    created_by_name: Optional[str] = None
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


# --- Notification — per-user in-app notification row. ---
class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    title: str
    body: str
    bug_id: Optional[int] = None
    event_id: Optional[int] = None
    actor_name: str
    read_at: Optional[datetime] = None
    created_at: datetime


class UnreadCountOut(BaseModel):
    unread: int


# --- Web push (Firebase Cloud Messaging) ---
class PushSubscribeIn(BaseModel):
    """A browser/device registering its FCM token for push."""
    token: str = Field(min_length=1, max_length=512)
    platform: str = Field(default="web", max_length=20)
    user_agent: str = Field(default="", max_length=400)


class PushUnsubscribeIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class PushConfigOut(BaseModel):
    """Public Firebase web config for the browser messaging SDK. All values are
    publishable; the service-account secret stays on the backend. `enabled` is
    False when push isn't configured."""
    enabled: bool
    api_key: str = ""
    auth_domain: str = ""
    project_id: str = ""
    messaging_sender_id: str = ""
    app_id: str = ""
    vapid_key: str = ""
