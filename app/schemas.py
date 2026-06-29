"""Pydantic request/response schemas."""
from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# HTML sanitizer for rich-text fields (description, comment body)
#
# The contenteditable editor emits HTML. Storing it raw would be a stored-XSS
# bug, so we sanitize on the server before writing to the database. The
# allowlist covers only the tags the editor produces, plus <img> for pasted
# screenshots (the editor base64-encodes pastes into data: URLs on src).
#
# We use a hand-rolled allowlist parser rather than bleach because bleach
# drags in html5lib (~200 KB of extra deps) and our tag set is small enough
# not to need it. If the allowed tags grow substantially, replace
# sanitize_html() with a bleach call — the interface is the same.
# ---------------------------------------------------------------------------
_ALLOWED_TAGS = {
    "p", "br", "div", "span",
    "b", "strong", "i", "em", "u", "s", "strike", "del", "ins",
    "ul", "ol", "li",
    "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "img",
}
_ALLOWED_ATTRS = {
    # Anything not listed here is stripped even for whitelisted tags — that's
    # what blocks <img onerror=...>. We don't accept `rel` on <a> because we
    # force our own rel="noopener nofollow" unconditionally below, preventing
    # a crafted rel="" from stripping the hardening.
    "a":   {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "code": {"class"},   # editor may emit `<code class="language-X">`
    "pre":  {"class"},
}
# Allowed URL schemes for href/src. data: is handled separately below.
_ALLOWED_URL_SCHEMES = ("http:", "https:", "mailto:", "/", "#")
# Only raster data: URLs are allowed. data:image/svg+xml is an XML document
# that can carry <script>/onload and executes in our origin, so it's excluded.
_DATA_IMAGE_RASTER_PREFIXES = (
    "data:image/png", "data:image/jpeg", "data:image/jpg",
    "data:image/gif", "data:image/webp", "data:image/bmp", "data:image/avif",
)
# RCDATA/CDATA elements in the HTML spec (script, style, etc.). Their tags are
# not on the allowlist, so we already drop the tags, but we also drop their
# text content rather than re-emitting it. This closes a parser-differential
# / mutation-XSS risk if any downstream consumer uses a real HTML5 tokenizer.
_RCDATA_DROP_TAGS = frozenset({
    "script", "style", "textarea", "title", "noscript", "xmp",
    "iframe", "noframes", "template",
})


class _HTMLAllowlistSanitizer(HTMLParser):
    """Drops every tag/attr not on the allowlist. Text content survives even
    when its parent tag is stripped."""
    # convert_charrefs=False preserves &amp; / &lt; as-is instead of collapsing
    # them into raw characters that would need re-escaping later.
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        # Tracks nesting depth inside RCDATA elements so their text is suppressed.
        self._drop_text_depth = 0

    def _safe_url(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        s = raw.strip()
        low = s.lower()
        # Explicit data:image/* (pasted screenshot), capped here to avoid
        # runaway storage. Upload-based attachments don't go through this
        # path; this is for inline pastes only.
        if low.startswith("data:image/"):
            # SVG is scriptable (can carry <script>/onload), so only raster
            # bitmap types pass. Also cap the size to avoid runaway storage;
            # this path is for inline pastes, not upload-based attachments.
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
        # Always inject rel on <a> (we don't accept the user-supplied one),
        # so reverse-tabnabbing hardening can't be stripped by the input.
        if t == "a":
            kept.append('rel="noopener nofollow"')
        return kept

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in _RCDATA_DROP_TAGS:
            # Track depth so we suppress content inside script/style/etc.
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
        # <br/> / <img .../> — re-emit as a start tag only. A self-closing RCDATA
        # tag (e.g. <script/>) opens and closes in one token; skip it without
        # touching _drop_text_depth so the counter doesn't get stuck.
        if tag.lower() in _RCDATA_DROP_TAGS:
            return
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._drop_text_depth > 0:
            # Inside a suppressed element — discard rather than re-emit.
            return
        self.out.append(
            data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")


def sanitize_html(value: Optional[str]) -> str:
    """Return sanitized HTML suitable for storage and display.

    Strips every tag and attribute not on the allowlist. Text content
    always survives. Idempotent: sanitize(sanitize(x)) == sanitize(x).
    Returns an empty string for None input."""
    if value is None:
        return ""
    s = str(value)
    p = _HTMLAllowlistSanitizer()
    p.feed(s)
    p.close()
    return "".join(p.out)


# Status sets are per item type so workflow terms only appear where they make
# sense ("Not a Bug" is Bug-only; "Blocked"/"Done" are Task-only; "Approved"/
# "Implemented" are Requirement-only). "New" appears in every list so the
# default value on create is always valid regardless of type.
#
# Rows with a status that is no longer valid for their current item_type are
# not rejected on read — they display as-is and can be updated to a valid
# value — but create/update calls that send an out-of-set status are rejected
# by the model validator below.
#
# ALLOWED_STATUSES is the union used by type-agnostic filter endpoints.
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
    """Return the valid statuses for the given item_type.

    Falls back to the Bug list for unknown types so rows predating the
    item_type column still have a valid status set."""
    return STATUSES_BY_TYPE.get(item_type or "Bug", STATUSES_BY_TYPE["Bug"])
ALLOWED_PRIORITIES = ["Low", "Medium", "High", "Critical"]
ALLOWED_ENVIRONMENTS = ["DEV", "UAT", "PROD"]
# The bugs table holds three work-item flavors:
#   Bug          — defects (the original use case)
#   Requirement  — features / specs / stories
#   Task         — standup tasks, typically one per team member per day
# Type is a classifier for filtering and badges; all other fields apply to all
# three flavors.
ALLOWED_ITEM_TYPES = ["Bug", "Requirement", "Task"]
ALLOWED_ROLES = ["admin", "manager", "user"]
# Link relationship kinds. Stored on the directed source→target edge; the route
# renders the inverse label on the target side (e.g. "blocks" → "is blocked by").
ALLOWED_LINK_TYPES = ["relates", "blocks", "duplicate"]
# Bulk operations the multi-select toolbar can request. Each goes through the
# same permission / audit / notification path as the equivalent single-item op.
ALLOWED_BULK_ACTIONS = [
    "set_status", "set_priority", "set_environment", "delete",
]
MIN_PASSWORD_LENGTH = 8
MIN_TITLE_LENGTH = 3
MIN_NAME_LENGTH = 2
MIN_PROJECT_NAME_LENGTH = 2


def normalize_choice(value: str, allowed: list[str], label: str) -> str:
    """Case-insensitive lookup against `allowed`; returns the canonical form.

    Called from both create/update validators and filter routes so all
    callers accept the same casings."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid {label}. Allowed: {', '.join(allowed)}")
    needle = value.strip().lower()
    for canonical in allowed:
        if canonical.lower() == needle:
            return canonical
    raise ValueError(f"Invalid {label}. Allowed: {', '.join(allowed)}")


# Private alias kept for callers within this module.
_normalize_choice = normalize_choice


# Validates the local part (no leading/trailing/consecutive dots), domain labels
# (alphanumeric with internal hyphens only), and an alphabetic TLD. Rejects
# malformed addresses before they reach the DB or a password-reset send.
# Quantifiers are bounded to avoid ReDoS.
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
    """Strip surrounding whitespace, then enforce min length.

    Pydantic's Field(min_length=...) measures the raw pre-strip value, so
    '  a  ' would pass a min_length=3 check without this."""
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
    # 'changeme' is the factory default on many existing accounts; always
    # accept it so existing deployments aren't locked out after an upgrade
    # that raises PASSWORD_MIN_LENGTH above 8.
    if v.lower() == "changeme":
        return v
    # Hard upper bound as a DoS guard — bcrypt cost scales with input length.
    if len(v) > 200:
        raise ValueError("Password is too long")
    from app.config import get_settings  # local import avoids an import cycle
    settings = get_settings()
    min_len = max(1, settings.PASSWORD_MIN_LENGTH)
    if len(v) < min_len:
        raise ValueError(f"Password must be at least {min_len} characters")
    # We don't require special characters. NIST 800-63B §5.1.1.2 notes that
    # character-class mandates push users toward predictable substitutions
    # without meaningfully raising entropy. Length + letter + digit is enough.
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
    # Projects this user can access. A manager/user is restricted to these;
    # omitting them creates an untagged user who sees nothing until tagged.
    # Admin role ignores tags entirely. The route validates existence and the
    # creator's own access. max_length caps abusive payloads.
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
    # Admin password reset. If present, replaces the stored hash. Omit to
    # leave the password unchanged.
    password: Optional[str] = None
    # Replace project memberships. None/omit = unchanged; empty list = untag.
    # Route validates project existence and the editor's own access.
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
    # Project memberships (sorted by id). Populated by the route from
    # user_projects; defaults to [] on paths that skip that join. An empty
    # list means untagged — non-admin users see nothing; admins see everything.
    project_ids: list[int] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    # Both fields are bounded so an unauthenticated caller can't POST a huge
    # body and burn CPU in bcrypt / the sha256 pre-hash. 200 matches the
    # new-password cap (no stored hash can exceed it); 254 = RFC 5321 email max.
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)


class ChangePasswordIn(BaseModel):
    # Accept any non-empty current password and let bcrypt-verify decide.
    # We can't introspect historical hashes, so we just guard against empty input.
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _check_password_strength(v)


class ForgotPasswordIn(BaseModel):
    # Length cap mirrors LoginIn — same DoS risk on an unauthenticated endpoint.
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)


class ResetPasswordIn(BaseModel):
    # The token is a hex SHA-256 value; cap it so a huge body can't be
    # hashed / compared unnecessarily.
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
    # Rich HTML description. 1 MB ceiling so several inline pasted screenshots
    # (base64 data URLs) can fit. Sanitized by the field validator below.
    description: str = Field(default="", max_length=1_000_000)
    reporter_id: Optional[int] = None
    assignee_ids: list[int] = Field(default_factory=list, max_length=200)
    item_type: str = Field(default="Bug")
    status: str = Field(default="New")
    priority: str = Field(default="Medium")
    environment: str = Field(default="DEV")
    due_date: Optional[str] = None
    # Link to an event (e.g. today's standup) at creation time.
    event_id: Optional[int] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        # Sanitize HTML from the SPA editor before storage. Also strip outer
        # whitespace so an empty body like "<p><br></p>" compares cleanly
        # against empty-string checks downstream.
        if not isinstance(v, str): return v
        return sanitize_html(v.strip())

    @field_validator("item_type")
    @classmethod
    def _check_item_type(cls, v: str) -> str:
        return _normalize_choice(v, ALLOWED_ITEM_TYPES, "item_type")

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        # Checks against the global union here. The per-type check runs in
        # _check_status_for_type below once item_type is also available.
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
        # Cross-field check: status must be valid for the chosen item_type.
        # "New" is in every set so the default always passes.
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
    # Set to an int to link to an event, or null/0 to unlink. The route uses
    # Pydantic's exclude_unset to distinguish "clearing" from "not provided".
    event_id: Optional[int] = None

    # Optimistic concurrency (opt-in). The client can echo the item's
    # updated_at as it last saw it; a mismatch returns 409 instead of silently
    # clobbering a concurrent edit. Omitting it keeps last-write-wins behaviour.
    expected_updated_at: Optional[str] = Field(default=None, max_length=64)

    # Preferred concurrency token: the item's integer version counter. More
    # reliable than expected_updated_at (sub-second precision, no clock skew).
    # Omitting both fields preserves last-write-wins.
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


# ---------------------------------------------------------------------------
# Comment / Activity / Detail
# ---------------------------------------------------------------------------
class CommentIn(BaseModel):
    # 200 KB ceiling so a pasted screenshot (base64 data URL) fits.
    # Dangerous payloads are stripped by the sanitizer regardless.
    body: str = Field(min_length=1, max_length=200_000)

    @field_validator("body")
    @classmethod
    def _strip(cls, v: str) -> str:
        # Sanitize server-side regardless of the editor's behaviour (defense
        # in depth against stored XSS). Then require that the result contains
        # either visible text or an <img> with a src — a body that's all tags
        # and whitespace after sanitization is treated as empty.
        if not isinstance(v, str):
            raise ValueError("Comment body must be a string")
        cleaned = sanitize_html(v.strip())
        text_only = re.sub(r"<[^>]+>", "", cleaned).strip()
        # A src-less <img> (stripped by the sanitizer) shouldn't count as
        # non-empty, so check for the attribute explicitly.
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


# ---------------------------------------------------------------------------
# Item linking
# ---------------------------------------------------------------------------
class BugLinkIn(BaseModel):
    target_bug_id: int
    link_type: str = "relates"

    @field_validator("link_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        return _normalize_choice(v, ALLOWED_LINK_TYPES, "link_type")


class BugLinkOut(BaseModel):
    """One link from a given bug's perspective. `direction` is "outgoing" when
    this bug is the source and "incoming" when it's the target. `label` is the
    human-readable phrasing from this side (e.g. stored "blocks" shows as
    "is blocked by" on the target)."""
    id: int
    link_type: str
    direction: str          # "outgoing" | "incoming"
    label: str
    other_bug_id: int
    other_bug_title: str
    other_bug_status: str
    other_bug_item_type: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Bulk actions — the multi-select toolbar on the list view.
# ---------------------------------------------------------------------------
class BulkActionIn(BaseModel):
    action: str
    ids: list[int] = Field(min_length=1, max_length=500)
    # Value for set_status / set_priority / set_environment. No valid value
    # exceeds 20 chars; the route still normalizes against the allowed set.
    value: Optional[str] = Field(default=None, max_length=20)
    # Optional {bug_id: version} map for optimistic concurrency. When provided,
    # rows whose version drifted are reported as conflicts and left unchanged.
    # Omit to keep last-write-wins behaviour.
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


# ---------------------------------------------------------------------------
# Sessions (admin only)
# ---------------------------------------------------------------------------
class SessionOut(BaseModel):
    """One row in the admin "active sessions" panel.

    `is_current` marks the session the admin is using right now, so the UI
    can label it and disable the revoke button. The API also rejects
    self-revocation; see routes/sessions.py.
    """
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


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
class StatsOut(BaseModel):
    # Total excluding "Not a Bug".
    bugs: int
    # KPI strip buckets on the dashboard.
    open: int
    resolved: int
    closed: int
    resolve_later: int
    # Kept for backward compatibility with older clients and external integrations.
    # The UI no longer renders these, but removing them would break cached frontends.
    projects: int = 0
    users: int = 0
    by_status: dict[str, int]
    by_priority: dict[str, int]
    by_environment: dict[str, int]
    by_type: dict[str, int] = Field(default_factory=dict)
    by_project: list[dict[str, Any]]
    by_assignee: list[dict[str, Any]]
    timeline: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Events — container for a group of work items (standup / sprint meeting).
# ---------------------------------------------------------------------------
class EventCreate(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=10000)
    scheduled_for: Optional[str] = None  # YYYY-MM-DD
    # Owning project. Scopes visibility — non-admin users only see events for
    # their projects. Optional at the API level so a project-less event is
    # valid (admins only). The SPA always sends one. When provided, the route
    # validates existence and the creator's access.
    project_id: Optional[int] = None
    # Admin/manager users to notify on event create/update/delete. Does not
    # include notifications for individual tasks filed under the event.
    # Empty list = no extra recipients beyond the creator.
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
    # Move to a different project. None/omit = leave unchanged. Route validates
    # existence and the editor's access to the target project.
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
    # Nullable so events created before this column was added still serialize.
    # project_name is resolved by the route for display.
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    created_by_user_id: Optional[int] = None
    created_by_name: Optional[str] = None
    item_count: int = 0
    assignee_count: int = 0
    # Full briefs so the UI can render names/emails without an extra round-trip.
    managers: list[UserBrief] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class EventDetail(EventOut):
    """Event with its full item list, returned by /api/events/{id}."""
    items: list[BugOut] = Field(default_factory=list)
    # Set when the item list was capped at the server ceiling, so the client
    # can show "showing N of M" rather than a silently truncated view.
    items_truncated: bool = False


# ---------------------------------------------------------------------------
# Notification — per-user in-app notification row.
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Web push (Firebase Cloud Messaging)
# ---------------------------------------------------------------------------
class PushSubscribeIn(BaseModel):
    """A browser/device registering its FCM token for push."""
    token: str = Field(min_length=1, max_length=512)
    platform: str = Field(default="web", max_length=20)
    user_agent: str = Field(default="", max_length=400)


class PushUnsubscribeIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class PushConfigOut(BaseModel):
    """Public Firebase web config for the browser messaging SDK.

    All values here are publishable (they appear in any Firebase web app).
    The secret — the server-side service-account JSON — never leaves the
    backend. `enabled` is False when push isn't configured, so the frontend
    can hide the toggle without a separate capability check.
    """
    enabled: bool
    api_key: str = ""
    auth_domain: str = ""
    project_id: str = ""
    messaging_sender_id: str = ""
    app_id: str = ""
    vapid_key: str = ""
