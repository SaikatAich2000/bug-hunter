"""Pydantic schemas (request/response DTOs)."""
from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# HTML sanitizer for rich-text fields (description, comment body)
#
# The contenteditable rich-text editor emits HTML (bold/italic/underline/
# lists/quotes/code/images). Storing the HTML preserves formatting on read, but
# rendering it unsanitized would be a stored-XSS bug: a user could paste
# `<script>` or an `onerror` attribute and run arbitrary JS for every viewer.
#
# Sanitizing on the server before storing keeps the database holding clean
# HTML. The allowlist is tight: only the formatting tags the editor produces,
# plus inline `<img>` so pasted screenshots survive the round-trip (the editor
# base64-encodes pastes into a `data:` URL on src, which is whitelisted).
#
# An in-house sanitizer is used rather than `bleach` because bleach pulls in
# `html5lib` and ~200 KB of dependency surface; for this constrained tag set a
# small allowlist parser is enough. If the formatting grows past this, swap in
# bleach behind the same `sanitize_html()` interface.
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
    # Per-tag attr allowlist. Anything missing here is stripped, even for
    # whitelisted tags, which is what blocks `<img onerror=...>` and the like.
    # `rel` is not accepted on <a>; we force our own rel="noopener nofollow"
    # below so a user-supplied rel="" / rel="opener" can't strip the hardening.
    "a":   {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "code": {"class"},   # editor sometimes emits `<code class="language-X">`
    "pre":  {"class"},
}
# Schemes allowed on `href` / `src`. `data:` is allowed only for image
# pastes; we check the URL scheme + MIME prefix together below.
_ALLOWED_URL_SCHEMES = ("http:", "https:", "mailto:", "/", "#")
# Only true raster image data: URLs survive sanitization (not svg+xml, which is
# scriptable). Compared against the lower-cased URL prefix.
_DATA_IMAGE_RASTER_PREFIXES = (
    "data:image/png", "data:image/jpeg", "data:image/jpg",
    "data:image/gif", "data:image/webp", "data:image/bmp", "data:image/avif",
)
# Tags whose CONTENT is CDATA/RCDATA in the HTML spec (their inner text is not
# markup). They're not on the allowlist, so the tags are dropped — but we also
# drop their text content entirely rather than escaping and re-emitting it,
# closing a parser-differential / mutation-XSS hazard if a consumer ever parses
# with a real HTML5 tokenizer.
_RCDATA_DROP_TAGS = frozenset({
    "script", "style", "textarea", "title", "noscript", "xmp",
    "iframe", "noframes", "template",
})


class _HTMLAllowlistSanitizer(HTMLParser):
    """Drops every tag/attr that isn't on the allowlist. Output is the
    surviving HTML; text content always survives even when the parent tag is
    stripped."""
    # convert_charrefs=False to re-emit `&amp;` / `&lt;` faithfully rather than
    # collapsing them into raw characters the next serialiser would have to
    # re-escape.
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        # Depth counter for CDATA/RCDATA elements whose text content we suppress.
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
            # Raster images only. data:image/svg+xml is an XML document that can
            # carry <script>/onload and executes in our origin if navigated to,
            # so it must not survive sanitization; only true bitmap types do.
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
        """Build the surviving `name="value"` attribute strings for tag `t`."""
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
            # Escape the attribute value for HTML embedding so it can't
            # contain quotes or angle brackets.
            v_safe = (v.replace("&", "&amp;").replace("<", "&lt;")
                       .replace(">", "&gt;").replace('"', "&quot;"))
            kept.append(f'{k}="{v_safe}"')
        # Force our own rel on every <a> (user rel isn't accepted), so the
        # reverse-tabnabbing/nofollow hardening can't be stripped by the input.
        if t == "a":
            kept.append('rel="noopener nofollow"')
        return kept

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in _RCDATA_DROP_TAGS:
            # Enter a script/style/etc. block: drop the tag AND its text content.
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
        # Void elements don't take a closing tag.
        if t in ("br", "img"):
            return
        self.out.append(f"</{t}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # `<br/>` / `<img .../>` — re-emit as start-tag only. A self-closing
        # RCDATA tag (e.g. <script/>) opens and closes in one token, so don't
        # leave the suppression depth stuck on.
        if tag.lower() in _RCDATA_DROP_TAGS:
            return
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._drop_text_depth > 0:
            # Inside a dropped script/style/etc. element — discard its content
            # entirely rather than escaping and re-emitting it as text.
            return
        self.out.append(
            data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")


def sanitize_html(value: Optional[str]) -> str:
    """Return a safe-for-display HTML string. Empty input → empty
    string. Removes every tag/attr outside the allowlist; text content
    survives. Idempotent: sanitize(sanitize(x)) == sanitize(x)."""
    if value is None:
        return ""
    s = str(value)
    p = _HTMLAllowlistSanitizer()
    p.feed(s)
    p.close()
    return "".join(p.out)


# The status set is per-item-type so a workflow term only applies to the work
# flavor where it makes sense ("Not a Bug" is a Bug-only verdict; "Blocked" /
# "Done" are Task-only; "Approved" / "Implemented" are Requirement-only). The
# single shared status, present in every list, is "New", so any row that
# defaults to "New" on create stays valid for its item_type without a data
# fix-up. Existing rows holding a status no longer valid for their current
# item_type aren't rejected on read — they're displayed as-is and can be
# updated to a valid value — but updates that try to move to an invalid value
# are rejected by the route layer.
#
# ALLOWED_STATUSES is the union, used by the filter endpoints and any client
# that posts a status without an item_type. The per-type sets below are the
# source of truth for create/update validation.
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
# Union of every status anywhere in the system. Used by the list filter
# endpoint (?status=…) which is type-agnostic, and by the legacy single
# global status dropdown some external clients still rely on.
ALLOWED_STATUSES = list(
    dict.fromkeys(s for sts in STATUSES_BY_TYPE.values() for s in sts)
)
# Statuses excluded from the "Total bugs" KPI: a "Not a Bug" verdict means the
# report isn't really a bug and shouldn't be counted. Only Bugs can carry this
# status, so the exclusion applies cleanly to Bug-tab analytics.
EXCLUDED_FROM_TOTAL_STATUSES = ["Not a Bug"]


def statuses_for_type(item_type: str) -> list[str]:
    """Return the valid status list for a given item_type, falling back to
    the Bug list if the type is unknown (preserves legacy behaviour for
    rows from before this column existed)."""
    return STATUSES_BY_TYPE.get(item_type or "Bug", STATUSES_BY_TYPE["Bug"])
ALLOWED_PRIORITIES = ["Low", "Medium", "High", "Critical"]
ALLOWED_ENVIRONMENTS = ["DEV", "UAT", "PROD"]
# Work-item types. The "bugs" table now holds three flavors of work:
#   Bug          - defects (the original use case)
#   Requirement  - feature / spec / story (Jira-style)
#   Task         - daily standup todo, usually assigned per team-member-per-day
# Environment / priority / status fields apply across all three; the type
# is purely a classifier that drives filtering, badges and the standup view.
ALLOWED_ITEM_TYPES = ["Bug", "Requirement", "Task"]
ALLOWED_ROLES = ["admin", "manager", "user"]
# Item-linking relationship kinds. Stored on the directed source->target edge;
# the route layer renders the inverse label for the target's view.
ALLOWED_LINK_TYPES = ["relates", "blocks", "duplicate"]
# Bulk operations the multi-select toolbar can request. Each reuses the same
# permission + audit + notification path as its single-item endpoint.
ALLOWED_BULK_ACTIONS = [
    "set_status", "set_priority", "set_environment", "delete",
]
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


# Local part has no leading/trailing/consecutive dots; each domain label is
# alphanumeric with only internal hyphens; TLD is alphabetic. This rejects
# malformed addresses (leading/consecutive dots, hyphen-bounded labels) that
# would otherwise be stored and used to send password-reset mail. Quantifiers
# are bounded so the pattern is not vulnerable to ReDoS.
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
    # Backwards-compat exception: 'changeme' (case-insensitive) is the default
    # password present in existing deployments for many accounts. It is always
    # accepted, and must stay above the length/complexity checks so it keeps
    # working even when PASSWORD_MIN_LENGTH is raised above 8 characters.
    if v.lower() == "changeme":
        return v
    # Upper bound is a DoS guard (bcrypt cost grows with input) — always on.
    if len(v) > 200:
        raise ValueError("Password is too long")
    # Configurable strength rules (app/config). Defaults: min length 8,
    # letter+digit required.
    from app.config import get_settings  # local import avoids an import cycle
    settings = get_settings()
    min_len = max(1, settings.PASSWORD_MIN_LENGTH)
    if len(v) < min_len:
        raise ValueError(f"Password must be at least {min_len} characters")
    # Special characters are not required: NIST 800-63B §5.1.1.2 finds
    # character-class rules push users toward predictable substitutions without
    # raising real entropy. Length + letter + digit is the middle ground.
    if settings.PASSWORD_REQUIRE_COMPLEXITY:
        has_letter = any(c.isalpha() for c in v)
        has_digit = any(c.isdigit() for c in v)
        if not (has_letter and has_digit):
            raise ValueError("Password must contain at least one letter and one number")
    # Block a small list of obviously-terrible passwords (exact, case-insensitive).
    if v.lower() in {"password", "password1", "password123", "admin123",
                     "qwerty123", "12345678a", "letmein123"}:
        raise ValueError("Password is too common — please choose a stronger one")
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
    # Bound both fields so an unauthenticated caller can't POST a multi-megabyte
    # password and burn CPU in the sha256 pre-hash (verify_password). 200 matches
    # the new-password cap; no stored password can exceed it. 254 = RFC email max.
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)

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
    # Cap the length like LoginIn so an unauthenticated caller can't post a
    # multi-megabyte "email" that the regex / DB query / audit line then process.
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)


class ResetPasswordIn(BaseModel):
    # The token is a hex SHA-256 lookup key; bound it so an arbitrarily large
    # body can't be hashed/compared.
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
    # Description is rich HTML; up to 1 MB so multiple inline pasted
    # screenshots (base64 data URLs) fit. Sanitized below.
    description: str = Field(default="", max_length=1_000_000)
    reporter_id: Optional[int] = None
    assignee_ids: list[int] = Field(default_factory=list, max_length=200)
    item_type: str = Field(default="Bug")
    status: str = Field(default="New")
    priority: str = Field(default="Medium")
    environment: str = Field(default="DEV")
    due_date: Optional[str] = None
    # Optional: file the item directly under an event (e.g. today's standup).
    event_id: Optional[int] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        # Description is rich HTML emitted by the SPA editor. Sanitize against
        # the allowlist before storage; strip surrounding whitespace so an
        # "empty" HTML body (e.g. "<p><br></p>") still round-trips as
        # effectively-empty for length checks downstream.
        if not isinstance(v, str): return v
        return sanitize_html(v.strip())

    @field_validator("item_type")
    @classmethod
    def _check_item_type(cls, v: str) -> str:
        return _normalize_choice(v, ALLOWED_ITEM_TYPES, "item_type")

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        # Type-aware status validation happens in the model_validator below;
        # this field-level check just confirms the value is in the global
        # union.
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
        # The status must belong to the chosen item_type's set. "New" is in
        # every set so the default value always passes regardless of type.
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
    # event_id can be set to an int to link this item to an event, or to
    # null/0 to unlink it. Pydantic's exclude_unset is what distinguishes
    # "not changing" from "explicitly clearing".
    event_id: Optional[int] = None

    # Optimistic concurrency (opt-in): the client may echo the item's
    # updated_at as it last saw it. If the item changed in the meantime the
    # update is rejected with 409 instead of silently clobbering the other
    # edit. Omitting it preserves last-write-wins for older clients.
    expected_updated_at: Optional[str] = Field(default=None, max_length=64)

    # Preferred optimistic-concurrency token: the item's integer ``version`` as
    # the client last saw it. Sub-second safe (unlike the whole-second
    # expected_updated_at). On mismatch the update is rejected with 409; omitting
    # both fields preserves last-write-wins.
    expected_version: Optional[int] = Field(default=None, ge=0)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: Optional[str]) -> Optional[str]:
        # See BugCreate.description — same sanitization on update.
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
    # Optional event link. Both fields nullable so standalone items
    # (no event) keep their existing shape.
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
    # Allow up to 200 KB so a pasted screenshot (base64 data URL) fits.
    # The HTML is sanitized below, so dangerous payloads are stripped
    # even if a client tries to abuse the larger ceiling.
    body: str = Field(min_length=1, max_length=200_000)

    @field_validator("body")
    @classmethod
    def _strip(cls, v: str) -> str:
        # Comments are rich HTML. Sanitize on the server to block stored-XSS
        # regardless of the SPA editor's behaviour, then verify the visible-
        # text length is at least 1 char so a whitespace-only post is
        # rejected.
        if not isinstance(v, str):
            raise ValueError("Comment body must be a string")
        cleaned = sanitize_html(v.strip())
        text_only = re.sub(r"<[^>]+>", "", cleaned).strip()
        # Treat the comment as non-empty only if it has visible text or a real
        # image (an <img> with a surviving src), so a bare content-less <img>
        # can't pass as non-empty and allow invisible/blank comment spam.
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
    """One link as seen FROM a given bug. `direction` is outgoing when this bug
    is the source, incoming when it's the target; `label` is the human phrasing
    from this bug's perspective (e.g. stored 'blocks' reads as 'is blocked by'
    on the target side)."""
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
    # status / priority / environment value for the set_* actions. Longest valid
    # value is well under 20 chars; the route still normalizes against the
    # allowed set, so this is just a Pydantic-layer bound.
    value: Optional[str] = Field(default=None, max_length=20)
    # Optional optimistic-concurrency map {bug_id: last_seen_version}. When
    # present, a row whose current version drifted is reported as a conflict and
    # left unchanged — so a bulk edit can't silently clobber a concurrent change.
    # Omitting it preserves last-writer-wins for callers that don't track it.
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
    # Rows left unchanged because their version drifted from expected_versions.
    conflicts: int = 0
    message: str = ""


class BugDetail(BugOut):
    comments: list[CommentOut] = Field(default_factory=list)
    activities: list[ActivityOut] = Field(default_factory=list)
    attachments: list[AttachmentBrief] = Field(default_factory=list)
    # Item links (both directions), rendered from this bug's perspective.
    links: list[BugLinkOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Sessions (admin only)
# ---------------------------------------------------------------------------
class SessionOut(BaseModel):
    """One row in the admin "active sessions" panel.

    `is_current` flags the session corresponding to the cookie the admin
    is currently using, so the UI can label it and disable the revoke
    button (the API also rejects revoking your own current session — see
    routes/sessions.py).
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
    # Total bugs, excluding the "Not a Bug" status per product requirement.
    bugs: int
    # Operational status buckets used by the KPI strip on the dashboard.
    open: int
    resolved: int
    closed: int
    resolve_later: int
    # Kept for backward compatibility with external integrations or cached
    # frontends that haven't reloaded yet. The UI no longer renders them, but
    # removing them outright would break out-of-date clients.
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
    # User IDs (admin or manager role) to set as event managers. These
    # users receive notifications when the event is created / updated /
    # deleted, but not when individual tasks inside the event are filed.
    # Empty list = no managers (the event has no notification recipients
    # beyond the creator).
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
    created_by_user_id: Optional[int] = None
    created_by_name: Optional[str] = None
    item_count: int = 0
    assignee_count: int = 0
    # Managers are returned as full briefs so the UI can render avatars
    # and emails without a second round-trip.
    managers: list[UserBrief] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class EventDetail(EventOut):
    """Event with its full item list — what /api/events/{id} returns."""
    items: list[BugOut] = Field(default_factory=list)
    # True when the item list was capped at the server's per-event ceiling, so
    # the client can show "showing N of M" instead of a silently lossy view.
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
    """Public Firebase web config handed to the browser to init messaging.

    All values here are *publishable* (they're embedded in any Firebase web
    app); the secret is the server-side service-account JSON, which never
    leaves the backend. ``enabled`` is False when web push isn't configured,
    so the frontend can hide the toggle cleanly.
    """
    enabled: bool
    api_key: str = ""
    auth_domain: str = ""
    project_id: str = ""
    messaging_sender_id: str = ""
    app_id: str = ""
    vapid_key: str = ""
