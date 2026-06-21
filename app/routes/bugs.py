"""Bugs API + comments + attachments + activity (per-bug)."""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException,
    Query, Request, UploadFile, status,
)
from fastapi.responses import Response
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth import (
    can_delete_attachment, can_delete_bug, can_delete_comment, can_edit_bug,
    can_edit_comment, get_current_user,
)
from app.database import get_db
from app.email_service import (
    BugSnapshot, UserSnapshot,
    notify_assignment, notify_bug_created, notify_bug_updated, notify_comment_added,
)
from app import notification_service
from app.image_strip import strip_image_metadata
from app.models import (
    Activity, Attachment, Bug, BugLink, Comment, Event, Project, User,
)
from app.schemas import (
    ALLOWED_ENVIRONMENTS, ALLOWED_ITEM_TYPES, ALLOWED_PRIORITIES,
    ALLOWED_STATUSES, ActivityOut, AttachmentBrief, BugCreate, BugDetail,
    BugLinkIn, BugLinkOut, BugListResponse, BugOut, BugUpdate, BulkActionIn,
    BulkActionResult, CommentIn, CommentOut, normalize_choice,
    statuses_for_type,
)

router = APIRouter(prefix="/api/bugs", tags=["bugs"])

# Repeated HTTPException detail strings, extracted so the wording stays
# consistent across endpoints.
_DETAIL_BUG_NOT_FOUND = "Bug not found"
_DETAIL_ATTACHMENT_NOT_FOUND = "Attachment not found"
_DEFAULT_MIME = "application/octet-stream"

# Soft cap on individual attachment size, protecting the DB from an oversized
# upload.
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

# Largest value the integer primary key can hold (32-bit signed, the Postgres
# `integer` type). A numeric search token longer than this overflows the column
# and raises a DataError on Postgres, so over-range digit strings are treated as
# free text rather than an id lookup.
_MAX_PK_INT = 2**31 - 1

# Read uploads in 1 MB chunks so over-sized requests are aborted before they
# consume RAM. Anything above MAX_FILE_BYTES is rejected mid-stream.
_UPLOAD_CHUNK = 1024 * 1024

# Per-user rate limit on attachment uploads. Without it, an authenticated user
# (or a stolen session) could chain 50 MB POSTs and bloat the DB. 20/min covers
# normal use while making automated abuse obvious.
_UPLOAD_RATE_WINDOW_SECONDS = 60
_UPLOAD_RATE_MAX = 20
_upload_buckets: dict[int, deque] = {}
_upload_rate_lock = threading.Lock()
# Bound the dict so a churn of users doesn't grow memory forever.
_UPLOAD_BUCKETS_MAX = 5_000

# Per-user comment rate limit. Each comment writes a row and fans out
# notifications and emails to the reporter and every assignee, so unbounded
# commenting is a notification/email-amplification vector. 30/min covers normal
# discussion.
_COMMENT_RATE_WINDOW_SECONDS = 60
_COMMENT_RATE_MAX = 30
_comment_buckets: dict[int, deque] = {}
_comment_rate_lock = threading.Lock()


def _check_user_rate(
    buckets: dict[int, deque], lock: threading.Lock, user_id: int,
    *, max_req: int, window: int, detail: str, cap: int = _UPLOAD_BUCKETS_MAX,
) -> None:
    """Per-user sliding-window rate guard; raises 429 when exceeded.

    Cheap, in-process, no Redis. Multi-worker deployments get per-worker
    buckets — for a tighter global limit put nginx limit_req in front.
    """
    now = time.monotonic()
    cutoff = now - window
    with lock:
        bucket = buckets.get(user_id)
        if bucket is None:
            if len(buckets) >= cap:
                buckets.pop(next(iter(buckets)), None)
            bucket = deque()
            buckets[user_id] = bucket
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_req:
            retry_after = max(1, int(window - (now - bucket[0])))
            raise HTTPException(
                status_code=429, detail=detail,
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


def _check_upload_rate(user_id: int) -> None:
    _check_user_rate(
        _upload_buckets, _upload_rate_lock, user_id,
        max_req=_UPLOAD_RATE_MAX, window=_UPLOAD_RATE_WINDOW_SECONDS,
        detail="Too many uploads, slow down a moment.",
    )


def _check_comment_rate(user_id: int) -> None:
    _check_user_rate(
        _comment_buckets, _comment_rate_lock, user_id,
        max_req=_COMMENT_RATE_MAX, window=_COMMENT_RATE_WINDOW_SECONDS,
        detail="Too many comments, slow down a moment.",
    )

# Content types that must not be served as-is, because a browser would render
# them inline and execute embedded scripts in our same-origin context. These
# are downgraded to application/octet-stream at download time and served with
# Content-Disposition: attachment so the browser saves rather than renders them.
_ACTIVE_CONTENT_TYPES = {
    "text/html", "application/xhtml+xml", "application/xml", "text/xml",
    "image/svg+xml", "application/javascript", "text/javascript",
    "application/x-javascript",
}

# MIME types a browser may render inline without executing anything. Everything
# outside this safelist is forced to Content-Disposition: attachment, since a
# blocklist alone can't keep up with every actively-rendered type. SVG is
# deliberately absent (scriptable) and is also in the blocklist above.
_INLINE_SAFE_PREFIXES = ("image/", "video/", "audio/")
_INLINE_SAFE_TYPES = {"application/pdf", "text/plain", "text/csv"}

# Executable / script extensions refused at upload time. This is the
# authoritative server-side denylist; the frontend blocklist is advisory and
# client-side only. Compared against the filename's final extension after
# stripping trailing dots/spaces (Windows trims those on download, so
# "evil.exe." would otherwise slip past a naive suffix check). .js is
# intentionally not blocked: plain JavaScript source is a legitimate
# attachment, and the download path already neutralizes active content
# (octet-stream + Content-Disposition: attachment + a sandboxed CSP). Windows
# script hosts (.jse/.vbs/.wsf/.ps1/etc.) and native executables are blocked.
_DANGEROUS_UPLOAD_EXTS = frozenset({
    "exe", "msi", "bat", "cmd", "com", "scr", "pif", "cpl", "hta", "jar",
    "jse", "vbs", "vbe", "wsf", "wsh", "ps1", "psm1", "sh", "bash",
    "app", "dmg", "pkg", "deb", "rpm", "apk", "msc", "reg", "lnk", "gadget",
    "dll", "sys", "elf",
})


def _dangerous_upload_ext(filename: str) -> Optional[str]:
    """Return the offending extension if the filename looks executable, else
    None. Strips trailing dots/whitespace before taking the extension."""
    name = (filename or "").strip().rstrip(". \t")
    if "." not in name:
        return None
    ext = name.rsplit(".", 1)[1].strip().lower()
    return ext if ext in _DANGEROUS_UPLOAD_EXTS else None


# Sanitize the filename when echoed in headers; the original is still kept in
# the DB. HTTP header values must be ASCII (RFC 7230), and RFC 6266 makes the
# plain `filename=` parameter US-ASCII only, with `filename*=` carrying any
# non-ASCII form via percent-encoding. ASCII is enforced here.
_HEADER_FILENAME_BAD = re.compile(r'[\r\n"\\]+')


def _safe_filename_for_header(name: str) -> str:
    """Return an ASCII-only, header-safe version of the filename.

    Strips CR/LF/quotes/backslashes that would break the Content-Disposition
    header and replaces any non-ASCII byte with an underscore. The original
    (possibly-Unicode) form is preserved on the wire via the RFC 5987
    ``filename*=`` parameter the caller appends (see ``download_attachment``).
    Without this ASCII pass, a non-ASCII filename would be Latin-1 encoded by
    the HTTP layer and arrive as garbage bytes or be rejected.
    """
    cleaned = _HEADER_FILENAME_BAD.sub("_", name)
    # Replace anything outside printable ASCII.
    ascii_only = "".join(c if 32 <= ord(c) < 127 else "_" for c in cleaned)
    return ascii_only or "file"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _item_type(bug: Bug) -> str:
    """Work-item flavour ('Bug' / 'Requirement' / 'Task'), defaulting rows with
    no item_type to 'Bug'."""
    return getattr(bug, "item_type", None) or "Bug"


def _user_brief(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role}


def _attachment_brief(a: Attachment) -> dict:
    return {
        "id": a.id, "filename": a.filename, "content_type": a.content_type,
        "size_bytes": a.size_bytes, "uploader_user_id": a.uploader_user_id,
        "uploader_name": a.uploader_name, "comment_id": a.comment_id,
        "created_at": a.created_at,
    }


def _bug_to_out_dict(bug: Bug, attachment_count: int = 0, can_edit: bool = False) -> dict:
    return {
        "id": bug.id,
        "project_id": bug.project_id,
        "project_name": bug.project.name if bug.project else None,
        "title": bug.title,
        "description": bug.description,
        "reporter": _user_brief(bug.reporter) if bug.reporter else None,
        "assignees": [_user_brief(a) for a in bug.assignees],
        "item_type": _item_type(bug),
        "status": bug.status,
        "priority": bug.priority,
        "environment": bug.environment,
        "due_date": bug.due_date,
        "event_id": getattr(bug, "event_id", None),
        "event_name": bug.event.name if getattr(bug, "event", None) else None,
        "created_at": bug.created_at,
        "updated_at": bug.updated_at,
        "version": getattr(bug, "version", 1),
        "attachment_count": attachment_count,
        "can_edit": can_edit,
    }


def _bug_snapshot(bug: Bug) -> BugSnapshot:
    return BugSnapshot(
        id=bug.id, title=bug.title,
        project_name=bug.project.name if bug.project else "",
        status=bug.status, priority=bug.priority, environment=bug.environment,
        description=bug.description,
        reporter=(UserSnapshot(id=bug.reporter.id, name=bug.reporter.name, email=bug.reporter.email)
                  if bug.reporter else None),
        assignees=tuple(UserSnapshot(id=a.id, name=a.name, email=a.email) for a in bug.assignees),
        # Default "Bug" lets rows with no item_type still render
        # correct-flavored emails without raising AttributeError.
        item_type=_item_type(bug),
        event_name=bug.event.name if getattr(bug, "event", None) else None,
    )


def _resolve_users(db: Session, user_ids: list[int]) -> list[User]:
    if not user_ids: return []
    rows = db.scalars(select(User).where(User.id.in_(user_ids))).all()
    found = {u.id for u in rows}
    missing = set(user_ids) - found
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown user ids: {sorted(missing)}")
    return list(rows)


def _resolve_user(db: Session, user_id: int | None) -> User | None:
    if user_id is None: return None
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=400, detail=f"User {user_id} does not exist")
    return user


def _reject_inactive(users: list[User], noun: str = "user") -> None:
    """Reject newly assigning/reporting work to a deactivated account: a
    disabled user can't log in, so routing notifications to them is a dead-end.
    Only new additions pass through here; an item that already references a
    since-deactivated user is left editable."""
    inactive = sorted((u.name for u in users if not u.is_active))
    if inactive:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot assign a deactivated {noun}: {', '.join(inactive)}",
        )


# Human phrasing for a stored link_type, per direction. Outgoing is the
# source's view of its own row; incoming is the target's (inverse) view.
_LINK_LABELS: dict[str, tuple[str, str]] = {
    # link_type: (outgoing label, incoming label)
    "relates": ("relates to", "relates to"),
    "blocks": ("blocks", "is blocked by"),
    "duplicate": ("duplicates", "is duplicated by"),
}


def _link_phrase(link_type: str, direction: str) -> str:
    out_label, in_label = _LINK_LABELS.get(link_type, (link_type, link_type))
    return out_label if direction == "outgoing" else in_label


def _serialize_link(link: BugLink, from_bug_id: int) -> dict:
    """Render a BugLink from the perspective of `from_bug_id`, picking the other
    end of the edge and the direction-appropriate label. Both FKs are NOT NULL
    and cascade-delete, so `other` is always present."""
    outgoing = link.source_bug_id == from_bug_id
    other = link.target if outgoing else link.source
    direction = "outgoing" if outgoing else "incoming"
    return {
        "id": link.id,
        "link_type": link.link_type,
        "direction": direction,
        "label": _link_phrase(link.link_type, direction),
        "other_bug_id": other.id,
        "other_bug_title": other.title,
        "other_bug_status": other.status,
        "other_bug_item_type": _item_type(other),
        "created_at": link.created_at,
    }


def _bug_links(db: Session, bug_id: int) -> list[dict]:
    """All links touching this bug (either direction), newest first, serialised
    from this bug's perspective."""
    rows = list(db.scalars(
        select(BugLink)
        .options(selectinload(BugLink.source), selectinload(BugLink.target))
        .where(or_(BugLink.source_bug_id == bug_id, BugLink.target_bug_id == bug_id))
        .order_by(BugLink.created_at.desc(), BugLink.id.desc())
    ).all())
    return [_serialize_link(link, bug_id) for link in rows]


def _log(
    db: Session, bug_id: int | None, actor: User | None, action: str, detail: str,
    entity_type: str = "bug", entity_id: int | None = None,
) -> None:
    db.add(Activity(
        bug_id=bug_id,
        entity_type=entity_type,
        entity_id=entity_id if entity_id is not None else bug_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action,
        detail=detail,
    ))


def _eager_bug() -> "select":
    return select(Bug).options(
        selectinload(Bug.project),
        selectinload(Bug.reporter),
        selectinload(Bug.assignees),
        selectinload(Bug.event),
    )


def _attachment_count(db: Session, bug_id: int) -> int:
    return db.scalar(
        select(func.count(Attachment.id)).where(Attachment.bug_id == bug_id)
    ) or 0


def _like_escape(needle: str) -> str:
    """Escape SQL LIKE wildcards so a user typing `_` or `%` matches the
    literal characters. Paired with `escape='\\\\'` on the LIKE clause."""
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
def _normalize_choice_list(values: Optional[list[str]], allowed: list[str], label: str) -> list[str]:
    """Normalize a multi-valued enum query param: strip empties and reject
    unknown values with 400."""
    if not values:
        return []
    out: list[str] = []
    for v in values:
        if v is None or v == "":
            continue
        try:
            out.append(normalize_choice(v, allowed, label))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return out


def _apply_where_both(stmt, count_stmt, clause):
    return stmt.where(clause), count_stmt.where(clause)


def _apply_q_filter(stmt, count_stmt, q: str):
    """Bug-id-or-text search: #123 / 123 → exact id match; otherwise LIKE."""
    q_clean = q.strip().lstrip("#")
    if q_clean.isascii() and q_clean.isdigit() and int(q_clean) <= _MAX_PK_INT:
        return _apply_where_both(stmt, count_stmt, Bug.id == int(q_clean))
    if not q_clean:
        return stmt, count_stmt
    # Match on the cleaned query: building the LIKE pattern from the raw `q`
    # would embed leading/trailing spaces and make `?q=  needle  ` never match.
    like = f"%{_like_escape(q_clean.lower())}%"
    clause = or_(
        func.lower(Bug.title).like(like, escape="\\"),
        func.lower(Bug.description).like(like, escape="\\"),
    )
    return _apply_where_both(stmt, count_stmt, clause)


def _reject_overflow_ids(**named_ids) -> None:
    """Reject id-valued query params outside a column's integer range.

    A bare numeric filter like ?reporter_id=99999999999999 would otherwise
    reach Postgres and raise a DataError (500). This mirrors the _MAX_PK_INT
    guard _apply_q_filter uses for the numeric `q` search, returning a clean 422
    instead. (SQLite tolerates big ints, so this only matters on Postgres.)"""
    for name, value in named_ids.items():
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            # Reject only values outside the int4 range. A small negative like
            # -1 is in range and simply matches nothing, so keep returning an
            # empty list for it rather than a 422.
            if v is not None and (v > _MAX_PK_INT or v < -_MAX_PK_INT - 1):
                raise HTTPException(status_code=422, detail=f"{name} is out of range")


def _apply_event_filter(stmt, count_stmt, event_id: int):
    """event_id=0 means "not in any event" — distinct from "any event"."""
    if event_id == 0:
        return _apply_where_both(stmt, count_stmt, Bug.event_id.is_(None))
    return _apply_where_both(stmt, count_stmt, Bug.event_id == event_id)


def _apply_list_filters(stmt, count_stmt, *, statuses, priorities, environments,
                        item_types, project_ids, assignee_ids, reporter_id,
                        due_date, event_id, q):
    """Layer every list_bugs filter onto the select+count statement pair."""
    if project_ids:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.project_id.in_(project_ids))
    if statuses:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.status.in_(statuses))
    if priorities:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.priority.in_(priorities))
    if environments:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.environment.in_(environments))
    if item_types:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.item_type.in_(item_types))
    if reporter_id is not None:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.reporter_id == reporter_id)
    if assignee_ids:
        stmt, count_stmt = _apply_where_both(
            stmt, count_stmt, Bug.assignees.any(User.id.in_(assignee_ids)),
        )
    if due_date:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.due_date == due_date)
    if event_id is not None:
        stmt, count_stmt = _apply_event_filter(stmt, count_stmt, event_id)
    if q:
        stmt, count_stmt = _apply_q_filter(stmt, count_stmt, q)
    return stmt, count_stmt


@router.get("", response_model=BugListResponse)
def list_bugs(
    project_id: Optional[list[int]] = Query(default=None),
    status_filter: Optional[list[str]] = Query(default=None, alias="status"),
    priority: Optional[list[str]] = Query(default=None),
    environment: Optional[list[str]] = Query(default=None),
    item_type: Optional[list[str]] = Query(default=None),
    reporter_id: Optional[int] = None,
    assignee_id: Optional[list[int]] = Query(default=None),
    event_id: Optional[int] = None,
    due_date: Optional[str] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> BugListResponse:
    """List bugs with filtering. Enum-like filters accept multiple values via
    repeated query params (?status=New&status=Resolved); a single-value call
    is parsed into a list of one and matched with `.in_(...)`."""
    if page < 1 or page_size < 1 or page_size > 200:
        raise HTTPException(status_code=400, detail="Invalid pagination parameters")
    _reject_overflow_ids(
        reporter_id=reporter_id, event_id=event_id,
        project_id=project_id, assignee_id=assignee_id,
    )

    statuses = _normalize_choice_list(status_filter, ALLOWED_STATUSES, "status")
    priorities = _normalize_choice_list(priority, ALLOWED_PRIORITIES, "priority")
    environments = _normalize_choice_list(environment, ALLOWED_ENVIRONMENTS, "environment")
    item_types = _normalize_choice_list(item_type, ALLOWED_ITEM_TYPES, "item_type")

    # Strip None / 0 from the int lists so callers can send blanks safely.
    project_ids = [p for p in (project_id or []) if p]
    assignee_ids = [a for a in (assignee_id or []) if a]

    stmt, count_stmt = _apply_list_filters(
        _eager_bug(), select(func.count(Bug.id)),
        statuses=statuses, priorities=priorities, environments=environments,
        item_types=item_types, project_ids=project_ids, assignee_ids=assignee_ids,
        reporter_id=reporter_id, due_date=due_date, event_id=event_id, q=q,
    )

    total = db.scalar(count_stmt) or 0
    offset = (page - 1) * page_size
    # Skip the query when the requested page is past the end: it returns the
    # correct empty page and avoids a huge OFFSET scan for a deep-page request
    # like ?page=10000000.
    if total and offset >= total:
        bugs: list[Bug] = []
    else:
        stmt = stmt.order_by(Bug.updated_at.desc(), Bug.id.desc()).limit(page_size).offset(offset)
        bugs = list(db.scalars(stmt).all())

    # One aggregate query for attachment counts keyed by bug_id, instead of an
    # N+1 per-bug count.
    bug_ids_in_page = [b.id for b in bugs]
    if bug_ids_in_page:
        att_counts = dict(db.execute(
            select(Attachment.bug_id, func.count(Attachment.id))
            .where(Attachment.bug_id.in_(bug_ids_in_page))
            .group_by(Attachment.bug_id)
        ).all())
    else:
        att_counts = {}

    items = []
    for b in bugs:
        items.append(_bug_to_out_dict(
            b,
            int(att_counts.get(b.id, 0)),
            can_edit_bug(_user, b.reporter_id, [a.id for a in b.assignees],
                         item_type=_item_type(b)),
        ))

    return BugListResponse.model_validate({
        "items": items,
        "page": page, "page_size": page_size,
        "total": total,
        "pages": (total + page_size - 1) // page_size if total else 0,
    })


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------
# Detail-view caps: the modal loads at most this many of the newest comments /
# activity rows, bounding the response for a long-lived item with unbounded
# history. Older history stays reachable via the dedicated /activity and
# /comments endpoints.
_DETAIL_COMMENTS_MAX = 500
_DETAIL_ACTIVITIES_MAX = 500
_DETAIL_ATTACHMENTS_MAX = 500


@router.get("/{bug_id}", response_model=BugDetail)
def get_bug(
    bug_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BugDetail:
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)

    # Bounded loads instead of the unbounded relationships. Newest first, which
    # matches the Bug.comments / Bug.activities relationship ordering.
    recent_comments = list(db.scalars(
        select(Comment).where(Comment.bug_id == bug_id)
        .order_by(Comment.created_at.desc(), Comment.id.desc())
        .limit(_DETAIL_COMMENTS_MAX)
    ).all())
    recent_activities = list(db.scalars(
        select(Activity).where(Activity.bug_id == bug_id)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
        .limit(_DETAIL_ACTIVITIES_MAX)
    ).all())

    # Pull all attachments (bug-level and comment-level), grouped per-comment,
    # newest first.
    all_atts = list(db.scalars(
        select(Attachment).where(Attachment.bug_id == bug_id)
        .order_by(Attachment.created_at.desc(), Attachment.id.desc())
        .limit(_DETAIL_ATTACHMENTS_MAX)
    ).all())
    by_comment: dict[int, list[Attachment]] = {}
    bug_level: list[Attachment] = []
    for a in all_atts:
        if a.comment_id is None:
            bug_level.append(a)
        else:
            by_comment.setdefault(a.comment_id, []).append(a)

    payload = _bug_to_out_dict(
        bug,
        # Exact count via aggregate; the loaded `all_atts` list is capped at
        # _DETAIL_ATTACHMENTS_MAX, so len() would under-report on busy items.
        _attachment_count(db, bug_id),
        can_edit_bug(user, bug.reporter_id, [a.id for a in bug.assignees],
                     item_type=_item_type(bug)),
    )
    payload["attachments"] = [_attachment_brief(a) for a in bug_level]
    payload["links"] = _bug_links(db, bug_id)
    payload["comments"] = []
    for c in recent_comments:
        payload["comments"].append({
            "id": c.id, "bug_id": c.bug_id,
            "author_user_id": c.author_user_id, "author_name": c.author_name,
            "body": c.body, "created_at": c.created_at,
            "attachments": [_attachment_brief(a) for a in by_comment.get(c.id, [])],
        })
    payload["activities"] = [
        {
            "id": a.id, "bug_id": a.bug_id, "entity_type": a.entity_type,
            "entity_id": a.entity_id, "actor_user_id": a.actor_user_id,
            "actor_name": a.actor_name, "action": a.action, "detail": a.detail,
            "created_at": a.created_at,
        }
        for a in recent_activities
    ]
    return BugDetail.model_validate(payload)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post("", response_model=BugOut, status_code=status.HTTP_201_CREATED)
def create_bug(
    payload: BugCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> BugOut:
    if db.get(Project, payload.project_id) is None:
        raise HTTPException(status_code=400, detail="Project does not exist")

    # Per-type policy: only admins/managers may create Tasks/Requirements
    # (regular users get Bugs only), the same rule can_edit_bug enforces on
    # edit, so a user can't create an item they could never edit.
    if not can_edit_bug(actor, actor.id, [], item_type=payload.item_type):
        raise HTTPException(
            status_code=403,
            detail=f"Only admins or managers can create a {payload.item_type.lower()}.",
        )

    # Reporter: if explicit one provided, only admin/manager can override.
    # Otherwise reporter = the current user.
    if payload.reporter_id is not None and payload.reporter_id != actor.id:
        if actor.role not in ("admin", "manager"):
            raise HTTPException(
                status_code=403,
                detail="You can only file items as yourself",
            )
        reporter = _resolve_user(db, payload.reporter_id)
    else:
        reporter = actor

    assignees = _resolve_users(db, payload.assignee_ids)
    # Don't route new work to a deactivated reporter/assignee.
    _reject_inactive([reporter, *assignees])

    # Validate optional event link.
    if payload.event_id is not None:
        if db.get(Event, payload.event_id) is None:
            raise HTTPException(status_code=400, detail="Event does not exist")

    bug = Bug(
        project_id=payload.project_id,
        title=payload.title,
        description=payload.description,
        reporter_id=reporter.id,
        item_type=payload.item_type,
        status=payload.status,
        priority=payload.priority,
        environment=payload.environment,
        due_date=payload.due_date,
        event_id=payload.event_id,
    )
    bug.assignees = list(assignees)
    db.add(bug)
    db.flush()
    _log(
        db, bug.id, actor, "bug_created",
        # Include the title so a future search for the title hits this row.
        f"{bug.item_type} #{bug.id} '{bug.title}' created with status '{bug.status}'.",
    )
    if assignees:
        names = ", ".join(a.name for a in assignees)
        _log(
            db, bug.id, actor, "assignees_added",
            f"Bug #{bug.id} '{bug.title}' assigned to: {names}",
        )
    # In-app notifications to the same recipients the emails target, written on
    # this session so they commit transactionally with the bug.
    _itype = _item_type(bug).lower()
    assignee_ids = [a.id for a in assignees]
    # No ``exclude`` here: a self-assignment still notifies the actor, since
    # being assigned is meaningful even when you assign yourself.
    notification_service.notify(
        db, assignee_ids, kind="assigned", background=background,
        title=f"Assigned to {_itype} #{bug.id}",
        body=f"{actor.name} assigned you to “{bug.title}”.",
        bug_id=bug.id, actor_name=actor.name,
    )
    if reporter.id != actor.id and reporter.id not in assignee_ids:
        notification_service.notify(
            db, [reporter.id], kind="reported", background=background,
            title=f"You're the reporter on {_itype} #{bug.id}",
            body=f"{actor.name} filed “{bug.title}” with you as reporter.",
            bug_id=bug.id, actor_name=actor.name,
        )
    db.commit()

    fresh = db.scalar(_eager_bug().where(Bug.id == bug.id))
    snap = _bug_snapshot(fresh)

    background.add_task(notify_bug_created, snap, actor.id)
    if assignees:
        background.add_task(
            notify_assignment, snap,
            tuple(UserSnapshot(id=a.id, name=a.name, email=a.email) for a in assignees),
            actor.name,
        )

    return BugOut.model_validate(_bug_to_out_dict(
        fresh, 0,
        can_edit_bug(actor, fresh.reporter_id, [a.id for a in fresh.assignees],
                     item_type=fresh.item_type),
    ))


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
_UPDATE_TRACKED_FIELDS = [
    "item_type", "status", "priority", "environment", "project_id",
    "due_date", "title", "description", "event_id",
]


def _validate_update_authorization(bug: Bug, actor: User) -> None:
    if not can_edit_bug(actor, bug.reporter_id, [a.id for a in bug.assignees],
                        item_type=_item_type(bug)):
        # Regular users cannot edit Tasks or Requirements.
        noun = _item_type(bug).lower()
        raise HTTPException(
            status_code=403,
            detail=f"You don't have permission to edit this {noun}.",
        )


# Link types whose reverse direction would contradict the forward one — e.g.
# "A blocks B" and "B blocks A" can't both be true. `relates` is symmetric and
# is intentionally excluded.
_DIRECTIONAL_LINK_TYPES = {"blocks", "duplicate"}


def _require_can_edit_link_endpoint(item: Bug, actor: User) -> None:
    """A link touches BOTH endpoints' relationship views, so adding or removing
    one requires edit rights on each endpoint — otherwise a user who can edit a
    Bug could attach/detach links on a Task/Requirement they cannot touch."""
    if not can_edit_bug(actor, item.reporter_id, [a.id for a in item.assignees],
                        item_type=_item_type(item)):
        noun = _item_type(item).lower()
        raise HTTPException(
            status_code=403,
            detail=f"You don't have permission to change links on this {noun}.",
        )


# Bound the cycle-detection walk so a pathological link graph can't turn a
# single link insert into an unbounded traversal.
_LINK_CYCLE_MAX_NODES = 10_000


def _directional_link_reaches(db: Session, start_id: int, goal_id: int, link_type: str) -> bool:
    """True if ``goal_id`` is reachable from ``start_id`` by following forward
    edges of ``link_type`` (bounded BFS). Used to detect cycles before insert."""
    seen = {start_id}
    frontier = [start_id]
    while frontier and len(seen) < _LINK_CYCLE_MAX_NODES:
        rows = db.execute(
            select(BugLink.target_bug_id).where(
                BugLink.source_bug_id.in_(frontier),
                BugLink.link_type == link_type,
            )
        ).all()
        nxt = []
        for (tid,) in rows:
            if tid == goal_id:
                return True
            if tid not in seen:
                seen.add(tid)
                nxt.append(tid)
        frontier = nxt
    return False


def _reject_inverse_directional_link(
    db: Session, source_id: int, target: Bug, link_type: str,
) -> None:
    """Block a directional link A->B that would create a cycle.

    Walk forward edges of the same type from the target: if the target can
    already reach the source, adding source->target closes a loop (A
    transitively blocked by itself), so reject. This covers both transitive
    cycles and the direct inverse B->A."""
    if link_type not in _DIRECTIONAL_LINK_TYPES:
        return
    if _directional_link_reaches(db, target.id, source_id, link_type):
        raise HTTPException(
            status_code=400,
            detail=(
                f"This would create a {link_type} cycle — #{target.id} already "
                f"{_link_phrase(link_type, 'outgoing')} #{source_id} "
                f"(directly or transitively)."
            ),
        )


def _authorize_item_type_change(fields: dict, bug: Bug, actor: User) -> None:
    """Over-post guard: re-authorize a client-supplied item_type change against
    the target type. _validate_update_authorization only checked the current
    type, so without this a regular user could PUT {"item_type": "Task"} to
    convert a Bug into a type they aren't allowed to edit.
    """
    new_type = fields.get("item_type")
    if new_type is None or new_type == _item_type(bug):
        return
    if not can_edit_bug(actor, bug.reporter_id,
                        [a.id for a in bug.assignees], item_type=new_type):
        raise HTTPException(
            status_code=403,
            detail=(
                "You don't have permission to convert this item to a "
                f"{new_type.lower()}."
            ),
        )


def _normalize_update_event_id(fields: dict, db: Session) -> None:
    if "event_id" in fields and fields["event_id"]:
        if db.get(Event, fields["event_id"]) is None:
            raise HTTPException(status_code=400, detail="Event does not exist")
    if "event_id" in fields and fields["event_id"] == 0:
        # Treat 0 as "unlink" for clients that can't easily send JSON null.
        fields["event_id"] = None


def _validate_update_status(fields: dict, bug: Bug) -> None:
    """Per-type status validation. Pydantic only checks the union; this checks
    the per-type set against the (possibly changing) type.

    When item_type changes, the effective status (the new one if supplied, else
    the current one) is re-validated against the new type, so converting e.g. a
    Resolved Bug into a Task can't persist a Task stuck on the Bug-only status
    'Resolved'. The tolerance for an already-invalid status the client re-sends
    unchanged only applies when the type isn't changing."""
    new_type = fields.get("item_type")
    type_changing = new_type is not None and new_type != _item_type(bug)
    has_new_status = "status" in fields and fields["status"] is not None
    if not has_new_status and not type_changing:
        return
    effective_type = new_type or _item_type(bug)
    effective_status = fields["status"] if has_new_status else bug.status
    allowed_for_type = statuses_for_type(effective_type)
    if effective_status in allowed_for_type:
        return
    # Invalid for the effective type. Tolerate an unchanged status only when the
    # type isn't changing (a row keeping its value); a type conversion must land
    # on a status valid for the new type.
    if not type_changing and has_new_status and effective_status == bug.status:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"Status '{effective_status}' is not valid for "
            f"{effective_type}. Allowed: {', '.join(allowed_for_type)}"
        ),
    )


def _validate_update_payload(fields: dict, bug: Bug, db: Session) -> None:
    if "project_id" in fields and fields["project_id"] is not None:
        if db.get(Project, fields["project_id"]) is None:
            raise HTTPException(status_code=400, detail="Project does not exist")
    _normalize_update_event_id(fields, db)
    _validate_update_status(fields, bug)


def _stale_edit_conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=("This item was changed by someone else since you opened it. "
                "Reload to see the latest, then reapply your change."),
    )


def _reject_if_updated_at_drifted(
    expected_updated_at: str, current_updated: datetime,
) -> None:
    """Timestamp path of the optimistic-concurrency check. Unparseable input is
    a hard 400 so a client that opts in gets a clear signal instead of silently
    losing the protection. current_updated is the locked row's updated_at (a
    NOT NULL column), so it's always present."""
    try:
        seen = datetime.fromisoformat(str(expected_updated_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="expected_updated_at is not a valid ISO-8601 timestamp.",
        ) from exc
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    current = current_updated
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    # updated_at is stored at whole-second resolution, so compare seconds.
    if int(seen.timestamp()) != int(current.timestamp()):
        raise _stale_edit_conflict()


def _enforce_optimistic_concurrency(
    db: Session, bug_id: int,
    expected_version: Optional[int],
    expected_updated_at: Optional[str],
) -> None:
    """Opt-in optimistic concurrency.

    Locks the item row (``FOR UPDATE``) so concurrent writers to the same item
    serialize, then rejects (409) when the client's last-seen ``version``
    (preferred, sub-second safe) or ``updated_at`` (whole-second) no longer
    matches the current value. No-op when the client sends neither field,
    preserving last-write-wins.
    """
    if expected_version is None and not expected_updated_at:
        return
    locked = db.execute(
        select(Bug.version, Bug.updated_at)
        .where(Bug.id == bug_id)
        .with_for_update()
    ).first()
    if locked is None:
        # Row vanished (concurrent delete); the caller's load will 404.
        return
    current_version, current_updated = locked
    if expected_version is not None:
        if int(expected_version) != int(current_version):
            raise _stale_edit_conflict()
        return
    _reject_if_updated_at_drifted(expected_updated_at, current_updated)


def _compute_tracked_changes(bug: Bug, fields: dict) -> list[tuple[str, str, str]]:
    """List of (field, old, new) tuples for every tracked field that differs.
    Description is included so a description-only edit isn't a no-op."""
    changes: list[tuple[str, str, str]] = []
    for f in _UPDATE_TRACKED_FIELDS:
        if f in fields and getattr(bug, f) != fields[f]:
            changes.append((f, str(getattr(bug, f) or ""), str(fields[f] or "")))
    return changes


def _apply_reporter_change(bug: Bug, db: Session, new_reporter_id: Optional[int],
                           changes: list[tuple[str, str, str]]) -> None:
    """Swap the reporter and append the audit row. Caller has already gated
    on permission."""
    old_reporter_label = bug.reporter.name if bug.reporter else "—"
    if new_reporter_id is None:
        bug.reporter_id = None
        new_reporter_label = "—"
    else:
        new_reporter = _resolve_user(db, new_reporter_id)
        _reject_inactive([new_reporter], "reporter")
        bug.reporter_id = new_reporter.id
        new_reporter_label = new_reporter.name if new_reporter else "—"
    if old_reporter_label != new_reporter_label:
        changes.append(("reporter", old_reporter_label, new_reporter_label))


def _apply_assignee_diff(bug: Bug, db: Session, assignee_ids: Optional[list[int]],
                        changes: list[tuple[str, str, str]]) -> tuple[list[User], list[User]]:
    """Diff and re-bind assignees if the set actually changed. Returns
    (newly-added, newly-removed) users so the caller can notify both; being
    unassigned is as meaningful as being assigned."""
    if assignee_ids is None:
        return [], []
    new_users = _resolve_users(db, assignee_ids)
    old_users = list(bug.assignees)
    old_ids = {a.id for a in old_users}
    new_ids = {u.id for u in new_users}
    added_ids = new_ids - old_ids
    removed_ids = old_ids - new_ids
    if not (added_ids or removed_ids):
        return [], []
    added = [u for u in new_users if u.id in added_ids]
    removed = [u for u in old_users if u.id in removed_ids]
    # Don't route new work to a deactivated account (only NEW additions).
    _reject_inactive(added)
    old_names = sorted(a.name for a in old_users)
    new_names = sorted(u.name for u in new_users)
    changes.append((
        "assignees",
        ", ".join(old_names) or "(none)",
        ", ".join(new_names) or "(none)",
    ))
    bug.assignees = new_users  # only re-bind when actually different
    return added, removed


def _persist_update(db: Session, bug: Bug, actor: User,
                    changes: list[tuple[str, str, str]]) -> None:
    """Commit when there are tracked changes; roll back otherwise so a no-op
    PUT doesn't bump updated_at."""
    if not changes:
        db.rollback()
        return
    prefix = f"#{bug.id} '{bug.title}' — "
    for field, old, new in changes:
        _log(
            db, bug.id, actor, f"{field}_changed",
            f"{prefix}{field}: '{old}' → '{new}'",
        )
    # Bump the optimistic-concurrency version on every committed change so a
    # second writer holding a stale copy is detected at sub-second resolution.
    # version is NOT NULL (server_default 1), so it's always an int.
    bug.version = bug.version + 1
    db.commit()


def _stage_update_notifications(
    db: Session, bug: Bug,
    changes: list[tuple[str, str, str]],
    newly_assigned: list[User], newly_removed: list[User],
    actor: User, background: BackgroundTasks,
) -> None:
    """Add the in-app notification rows for an update to the current session
    (no commit) so they persist atomically with the change in _persist_update's
    single commit, rather than in a second transaction a crash could drop."""
    new_ids = {u.id for u in newly_assigned}
    itype = _item_type(bug).lower()
    if changes:
        recipients = [
            uid for uid in [bug.reporter_id, *[a.id for a in bug.assignees]]
            if uid not in new_ids
        ]
        notification_service.notify(
            db, recipients, kind="updated", background=background,
            title=f"{itype.capitalize()} #{bug.id} updated",
            body=f"{actor.name} changed " + ", ".join(f for f, _, _ in changes) + ".",
            bug_id=bug.id, actor_name=actor.name, exclude=actor.id,
        )
    if newly_assigned:
        # No ``exclude``: a self-assignment still notifies the actor, since
        # they are now assigned to this item.
        notification_service.notify(
            db, list(new_ids), kind="assigned", background=background,
            title=f"Assigned to {itype} #{bug.id}",
            body=f"{actor.name} assigned you to “{bug.title}”.",
            bug_id=bug.id, actor_name=actor.name,
        )
    if newly_removed:
        # Being taken off an item is as meaningful as being added to one.
        notification_service.notify(
            db, [u.id for u in newly_removed], kind="updated", background=background,
            title=f"Unassigned from {itype} #{bug.id}",
            body=f"{actor.name} removed you from “{bug.title}”.",
            bug_id=bug.id, actor_name=actor.name, exclude=actor.id,
        )


def _schedule_update_notifications(background: BackgroundTasks, snap: BugSnapshot,
                                   changes: list[tuple[str, str, str]],
                                   newly_assigned: list[User], actor: User) -> None:
    if changes:
        background.add_task(
            notify_bug_updated, snap, list(changes), actor.name, actor.id,
        )
    if newly_assigned:
        background.add_task(
            notify_assignment, snap,
            tuple(UserSnapshot(id=u.id, name=u.name, email=u.email) for u in newly_assigned),
            actor.name,
        )


@router.put("/{bug_id}", response_model=BugOut)
def update_bug(
    bug_id: int,
    payload: BugUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> BugOut:
    fields = payload.model_dump(exclude_unset=True)
    expected_version = fields.pop("expected_version", None)
    expected_updated_at = fields.pop("expected_updated_at", None)

    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    # Authorize before the optimistic-concurrency check so an unauthorized
    # caller gets a clean 403, never a 409. Checking the version first would let
    # a caller with no edit rights probe a protected item's version/existence
    # via 409-vs-403 differential responses.
    _validate_update_authorization(bug, actor)
    # Optimistic concurrency (opt-in): lock the row and compare the client's
    # last-seen version/timestamp before mutating, so concurrent writers to the
    # same item serialize and the stale one gets a clean 409.
    _enforce_optimistic_concurrency(db, bug_id, expected_version, expected_updated_at)
    _validate_update_payload(fields, bug, db)
    # Over-post guard: block converting a Bug into a Task/Requirement the caller
    # can't edit (the auth check above only saw the CURRENT type).
    _authorize_item_type_change(fields, bug, actor)

    assignee_ids = fields.pop("assignee_ids", None)
    has_reporter_in_payload = "reporter_id" in fields
    new_reporter_id = fields.pop("reporter_id", None)

    # Only run the reporter-change gate when the reporter actually changes.
    reporter_actually_changes = (
        has_reporter_in_payload and new_reporter_id != bug.reporter_id
    )
    if reporter_actually_changes and actor.role not in ("admin", "manager"):
        raise HTTPException(
            status_code=403,
            detail="Only admins or managers can change the reporter",
        )

    changes = _compute_tracked_changes(bug, fields)
    for key, value in fields.items():
        setattr(bug, key, value)

    if reporter_actually_changes:
        _apply_reporter_change(bug, db, new_reporter_id, changes)
    newly_assigned, newly_removed = _apply_assignee_diff(bug, db, assignee_ids, changes)

    # Stage the in-app notifications on the same session before the persist
    # commit so the change, its activity rows, the version bump, and the
    # notifications all land in one transaction; otherwise a crash between two
    # commits could persist the change with no notification/digest fan-out.
    if changes or newly_assigned or newly_removed:
        _stage_update_notifications(
            db, bug, changes, newly_assigned, newly_removed, actor, background,
        )

    _persist_update(db, bug, actor, changes)

    fresh = db.scalar(_eager_bug().where(Bug.id == bug_id))
    snap = _bug_snapshot(fresh)
    _schedule_update_notifications(background, snap, changes, newly_assigned, actor)

    return BugOut.model_validate(_bug_to_out_dict(
        fresh, _attachment_count(db, bug_id),
        can_edit_bug(actor, fresh.reporter_id, [a.id for a in fresh.assignees],
                     item_type=fresh.item_type),
    ))


# ---------------------------------------------------------------------------
# Shared stakeholder notification for the secondary operations (delete,
# attachments, comment edit/delete, links). The create/update/assign paths
# above build their own tailored notifications; these reuse one helper so every
# mutating operation on an item reaches its reporter + assignees and, when
# EMAIL_DIGEST_ENABLED, is batched into the cron digest email (notify() leaves
# emailed_at NULL, so the digest job picks it up once).
# ---------------------------------------------------------------------------
def _notify_item_stakeholders(
    db: Session, bug: Bug, actor: User, *, kind: str, title: str, body: str,
    background: "BackgroundTasks | None" = None, link_bug: bool = True,
) -> None:
    """In-app notify a work item's reporter + assignees (minus the actor).
    Written on the caller's session; the caller commits.

    link_bug=False omits the bug deep-link, used by delete: the bug_id FK is
    ON DELETE CASCADE, so a notification pointing at the about-to-be-deleted bug
    would be cascaded away before the digest could mail it."""
    notification_service.notify(
        db, [bug.reporter_id, *[a.id for a in bug.assignees]],
        kind=kind, background=background, title=title, body=body,
        bug_id=bug.id if link_bug else None,
        actor_name=actor.name, exclude=actor.id,
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
@router.delete("/{bug_id}")
def delete_bug(
    bug_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict[str, str]:
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    # Item deletion is admin-only across every type; managers can edit but not
    # delete, and reporters/assignees cannot delete.
    if not can_delete_bug(actor, item_type=_item_type(bug)):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete items.",
        )
    title = bug.title
    itype = _item_type(bug)
    # Tell the reporter + assignees their item is gone before the delete; the
    # notification can't carry a bug deep-link (FK cascade), so link_bug=False.
    _notify_item_stakeholders(
        db, bug, actor, kind="updated", background=background, link_bug=False,
        title=f"{itype} #{bug_id} deleted",
        body=f"{actor.name} deleted “{title}”.",
    )
    # Detach the bug's audit history before deleting the bug so the trail
    # survives. Two-step:
    #   1. UPDATE activity_log SET bug_id = NULL WHERE bug_id = <id>
    #   2. DELETE FROM bugs WHERE id = <id>
    # This works whether the FK is ondelete="SET NULL" or a legacy
    # ondelete="CASCADE": by the time the DELETE fires no activity row
    # references this bug, so a cascade has nothing to remove. The rows keep
    # entity_id pointing at the now-gone bug, and their `detail` still carries
    # the original title, so the global audit search still works.
    db.execute(
        update(Activity)
        .where(Activity.bug_id == bug_id)
        .values(bug_id=None)
    )
    db.flush()
    db.delete(bug)
    # Add one summary row so the trail records the delete itself.
    db.add(Activity(
        bug_id=None, entity_type="bug", entity_id=bug_id,
        actor_user_id=actor.id, actor_name=actor.name,
        action="bug_deleted",
        detail=f"Deleted {itype.lower()} #{bug_id}: {title}",
    ))
    db.commit()
    return {"message": f"{itype} deleted"}


# ---------------------------------------------------------------------------
# Comments (with optional attachments)
# ---------------------------------------------------------------------------
@router.get("/{bug_id}/comments", response_model=list[CommentOut])
def list_comments(
    bug_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    if db.get(Bug, bug_id) is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    # Newest comments first (matches the detail endpoint's ordering).
    comments = list(db.scalars(
        select(Comment).where(Comment.bug_id == bug_id)
        .order_by(Comment.created_at.desc(), Comment.id.desc())
        .limit(_DETAIL_COMMENTS_MAX)
    ).all())
    atts = list(db.scalars(
        select(Attachment).where(Attachment.bug_id == bug_id, Attachment.comment_id.isnot(None))
    ).all())
    by_cid: dict[int, list[Attachment]] = {}
    for a in atts:
        by_cid.setdefault(a.comment_id, []).append(a)
    return [
        {
            "id": c.id, "bug_id": c.bug_id,
            "author_user_id": c.author_user_id, "author_name": c.author_name,
            "body": c.body, "created_at": c.created_at,
            "attachments": [_attachment_brief(a) for a in by_cid.get(c.id, [])],
        }
        for c in comments
    ]


@router.post("/{bug_id}/comments", response_model=CommentOut, status_code=status.HTTP_201_CREATED)
def add_comment(
    bug_id: int,
    payload: CommentIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    author: User = Depends(get_current_user),
) -> dict:
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    # Same per-type policy as edit/link: a regular user can discuss a Bug but
    # not a Task/Requirement they have no edit rights on.
    _validate_update_authorization(bug, author)
    # Rate-limit after the 404 and authz check so a probing/forbidden request
    # doesn't burn the caller's budget; it still bounds actual comment writes.
    _check_comment_rate(author.id)

    c = Comment(
        bug_id=bug_id,
        author_user_id=author.id,
        author_name=author.name,
        body=payload.body,
    )
    db.add(c)
    db.flush()
    _log(db, bug_id, author, "comment_added",
         f"#{bug.id} '{bug.title}' — comment by {author.name}: {payload.body[:80]}")
    # In-app notification to reporter + assignees, minus the author.
    notification_service.notify(
        db, [bug.reporter_id, *[a.id for a in bug.assignees]],
        kind="comment", background=background,
        title=f"New comment on #{bug.id}",
        body=f"{author.name} commented on “{bug.title}”.",
        bug_id=bug.id, actor_name=author.name, exclude=author.id,
    )
    db.commit()
    db.refresh(c)

    snap = _bug_snapshot(bug)
    background.add_task(
        notify_comment_added, snap, author.name, author.id, payload.body,
    )
    return {
        "id": c.id, "bug_id": c.bug_id,
        "author_user_id": c.author_user_id, "author_name": c.author_name,
        "body": c.body, "created_at": c.created_at, "attachments": [],
    }


# ---------------------------------------------------------------------------
# Attachments — upload, list, download, delete
# ---------------------------------------------------------------------------
async def _read_upload_with_limit(file: UploadFile, limit: int) -> bytes:
    """Stream the upload in chunks and abort early if it exceeds the limit, so
    an oversized request can't buffer its entire body into memory before the
    size is checked."""
    buf = bytearray()
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(
                status_code=413,
                detail=(
                    "This file is too large. The largest file you can attach "
                    f"is {limit // (1024 * 1024)} MB."
                ),
            )
    return bytes(buf)


@router.post("/{bug_id}/attachments", response_model=AttachmentBrief, status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    bug_id: int,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    comment_id: Optional[int] = Form(default=None),
    db: Session = Depends(get_db),
    uploader: User = Depends(get_current_user),
) -> dict:
    bug = db.get(Bug, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    # Same per-type policy as edit/link: regular users can attach to a Bug but
    # not to a Task/Requirement they can't edit.
    _validate_update_authorization(bug, uploader)
    if comment_id is not None:
        c = db.get(Comment, comment_id)
        if c is None or c.bug_id != bug_id:
            raise HTTPException(status_code=400, detail="Invalid comment_id for this bug")

    # Per-user rate limit: the upload endpoint writes up to a 50 MB BLOB per
    # call, so without this a hostile authenticated client could bloat the DB.
    # Checked after 404/authz/comment validation (so a probing or forbidden
    # request doesn't burn the budget) but before reading the body.
    _check_upload_rate(uploader.id)

    # Refuse executable/script attachments server-side (the client check is
    # advisory). Done before reading the body so a blocked upload doesn't stream
    # megabytes first.
    bad_ext = _dangerous_upload_ext(file.filename or "")
    if bad_ext is not None:
        raise HTTPException(
            status_code=400,
            detail=f"Files of type .{bad_ext} can't be attached for security reasons.",
        )

    data = await _read_upload_with_limit(file, MAX_FILE_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="This file is empty, so there's nothing to attach.")

    # Strip EXIF / GPS / camera-serial / XMP / ICC from raster image uploads.
    # No-op for non-images and fails open on errors so an exotic image format
    # doesn't block the upload.
    data = strip_image_metadata(data, file.content_type)
    # The cap was checked on the pre-strip bytes; re-assert it in case the
    # re-encode grew the file past the limit.
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "This file is too large. The largest file you can attach "
                f"is {MAX_FILE_BYTES // (1024 * 1024)} MB."
            ),
        )

    att = Attachment(
        bug_id=bug_id,
        comment_id=comment_id,
        uploader_user_id=uploader.id,
        uploader_name=uploader.name,
        filename=(file.filename or "unnamed")[:255],
        content_type=(file.content_type or _DEFAULT_MIME)[:120],
        size_bytes=len(data),
        data=data,
    )
    db.add(att)
    db.flush()
    _log(
        db, bug_id, uploader, "attachment_added",
        f"{uploader.name} uploaded '{att.filename}' ({len(data)} bytes)"
        + (f" on comment #{comment_id}" if comment_id else ""),
        entity_type="attachment", entity_id=att.id,
    )
    _notify_item_stakeholders(
        db, bug, uploader, kind="updated", background=background,
        title=f"Attachment added on #{bug_id}",
        body=f"{uploader.name} attached “{att.filename}” to “{bug.title}”.",
    )
    db.commit()
    db.refresh(att)
    return _attachment_brief(att)


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header: str | None, size: int) -> Optional[tuple[int, int]]:
    """Parse a single-range ``Range: bytes=start-end`` header.

    Returns an inclusive ``(start, end)`` byte span clamped to ``[0, size-1]``,
    or ``None`` when the header is absent/empty/unsatisfiable/multi-range (the
    caller then serves the full body). Browsers need this to seek a ``<video>``:
    without a 206 reply, setting ``currentTime`` snaps back to 0.
    """
    if not header or size <= 0:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None  # malformed or multi-range — serve whole body
    start_s, end_s = m.group(1), m.group(2)
    if not start_s and not end_s:
        return None
    if not start_s:
        # suffix range: last N bytes
        length = min(int(end_s), size)
        start, end = size - length, size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return None  # unsatisfiable — fall back to full body (200)
    return start, end


@router.get("/{bug_id}/attachments/{att_id}/download")
def download_attachment(
    bug_id: int, att_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    # Load metadata without the BLOB column: a range request must not pull the
    # whole (up to 50 MB) `data` into memory just to slice a few bytes, or a
    # video seek (which fires many small Range requests) becomes repeated
    # full-column reads. The bytes are fetched below, sliced in SQL for ranges
    # and read whole only for a full-body download.
    meta = db.execute(
        select(
            Attachment.bug_id, Attachment.filename,
            Attachment.content_type, Attachment.size_bytes,
        ).where(Attachment.id == att_id)
    ).first()
    if meta is None or meta[0] != bug_id:
        raise HTTPException(status_code=404, detail=_DETAIL_ATTACHMENT_NOT_FOUND)
    _att_bug_id, att_filename, att_content_type, att_size = meta

    # Decide content-type and disposition.
    #
    # Active types like text/html, image/svg+xml, and JS can carry executable
    # script; rendering them inline would run that script in our origin's
    # context (same-origin XSS via stored attachment). For those types, force
    # `attachment` disposition and downgrade the content-type to octet-stream so
    # the browser saves rather than executes.
    ct_lower = (att_content_type or "").lower().split(";")[0].strip()
    is_active = ct_lower in _ACTIVE_CONTENT_TYPES
    safe_ct = _DEFAULT_MIME if is_active else (att_content_type or _DEFAULT_MIME)
    inline_ok = not is_active and (
        ct_lower in _INLINE_SAFE_TYPES
        or ct_lower.startswith(_INLINE_SAFE_PREFIXES)
    )
    disposition = "inline" if inline_ok else "attachment"

    safe_fname = _safe_filename_for_header(att_filename)
    # RFC 5987 form for non-ASCII filenames; keeps a plain ASCII fallback.
    cd = (
        f'{disposition}; filename="{safe_fname}"; '
        f"filename*=UTF-8''{quote(att_filename, safe='')}"
    )

    headers = {
        "Content-Disposition": cd,
        # Browsers only attempt to seek <video>/<audio> when the server
        # advertises byte-range support; without it currentTime resets to 0.
        "Accept-Ranges": "bytes",
        # Opt this response out of GZipMiddleware: attachments are already
        # compressed media, and gzipping a 206 partial body corrupts the
        # byte-range math the player relies on to seek. An explicit
        # Content-Encoding makes Starlette's GZip responder pass us through.
        "Content-Encoding": "identity",
        # Defense-in-depth: even if some future code path ends up
        # serving HTML inline, these headers make it harder to weaponize.
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Frame-Options": "DENY",
        # Keep a Cache-Control here so the global middleware doesn't
        # try to override us — attachments may be private.
        "Cache-Control": "private, max-age=0, no-cache",
    }

    span = _parse_range(request.headers.get("range"), att_size)
    if span is not None:
        start, end = span
        length = end - start + 1
        # Slice in the database (1-indexed substr) so only the requested bytes
        # leave storage. Works on both Postgres (bytea) and SQLite (blob).
        raw = db.scalar(
            select(func.substr(Attachment.data, start + 1, length))
            .where(Attachment.id == att_id)
        )
        if raw is None:
            # Row was deleted between the metadata read and this slice; return a
            # clean 404 rather than a protocol-inconsistent empty 206.
            raise HTTPException(status_code=404, detail=_DETAIL_ATTACHMENT_NOT_FOUND)
        chunk = bytes(raw)
        headers["Content-Range"] = f"bytes {start}-{end}/{att_size}"
        headers["Content-Length"] = str(len(chunk))
        return Response(
            content=chunk,
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=safe_ct,
            headers=headers,
        )

    # Full download: fetch the whole column once. A plain Response sets an
    # accurate Content-Length (StreamingResponse drops the explicit one).
    data = db.scalar(select(Attachment.data).where(Attachment.id == att_id))
    return Response(content=bytes(data) if data is not None else b"",
                    media_type=safe_ct, headers=headers)


@router.delete("/{bug_id}/attachments/{att_id}")
def delete_attachment(
    bug_id: int, att_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    a = db.get(Attachment, att_id)
    if a is None or a.bug_id != bug_id:
        raise HTTPException(status_code=404, detail=_DETAIL_ATTACHMENT_NOT_FOUND)
    # Attachment deletion is admin-only for both bug-level and comment-level
    # attachments. Uploaders and managers cannot remove their own files.
    if not can_delete_attachment(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete attachments.",
        )
    fname = a.filename
    db.delete(a)
    _log(
        db, bug_id, actor, "attachment_deleted",
        f"Deleted attachment '{fname}'",
        entity_type="attachment", entity_id=att_id,
    )
    # The attachment's FK guarantees its bug exists (a cascade would have
    # removed the attachment otherwise), so no None-guard is needed here.
    bug = db.get(Bug, bug_id)
    _notify_item_stakeholders(
        db, bug, actor, kind="updated", background=background,
        title=f"Attachment removed on #{bug_id}",
        body=f"{actor.name} deleted attachment “{fname}” from “{bug.title}”.",
    )
    db.commit()
    return {"message": "Attachment deleted"}


# ---------------------------------------------------------------------------
# Comment edit / delete — admin only
# ---------------------------------------------------------------------------
@router.put("/{bug_id}/comments/{comment_id}", response_model=CommentOut)
def update_comment(
    bug_id: int, comment_id: int,
    payload: CommentIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    """Edit a comment's body. Admin only. The audit trail records who edited
    and the new body so an accidental rewrite is traceable."""
    if not can_edit_comment(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins can edit comments.",
        )
    c = db.get(Comment, comment_id)
    if c is None or c.bug_id != bug_id:
        raise HTTPException(status_code=404, detail="Comment not found")
    c.body = payload.body
    db.flush()
    _log(
        db, bug_id, actor, "comment_edited",
        f"Comment #{c.id} edited by {actor.name}: {payload.body[:80]}",
        entity_type="comment", entity_id=c.id,
    )
    # The comment's FK guarantees its bug exists, so use it directly.
    bug = db.get(Bug, bug_id)
    _notify_item_stakeholders(
        db, bug, actor, kind="comment", background=background,
        title=f"Comment edited on #{bug_id}",
        body=f"{actor.name} edited a comment on “{bug.title}”.",
    )
    db.commit()
    db.refresh(c)
    atts = list(db.scalars(
        select(Attachment).where(Attachment.comment_id == c.id)
        .order_by(Attachment.created_at.desc(), Attachment.id.desc())
        .limit(_DETAIL_ATTACHMENTS_MAX)
    ).all())
    return {
        "id": c.id, "bug_id": c.bug_id,
        "author_user_id": c.author_user_id, "author_name": c.author_name,
        "body": c.body, "created_at": c.created_at,
        "attachments": [_attachment_brief(a) for a in atts],
    }


@router.delete("/{bug_id}/comments/{comment_id}")
def delete_comment(
    bug_id: int, comment_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    """Delete a comment and its attachments (the FK on attachments cascades
    from comment_id). Admin only. The audit row keeps a preview of the deleted
    body for context."""
    if not can_delete_comment(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete comments.",
        )
    c = db.get(Comment, comment_id)
    if c is None or c.bug_id != bug_id:
        raise HTTPException(status_code=404, detail="Comment not found")
    preview = (c.body or "")[:80]
    author_name = c.author_name
    db.delete(c)
    _log(
        db, bug_id, actor, "comment_deleted",
        f"Comment #{comment_id} by {author_name} deleted: {preview}",
        entity_type="comment", entity_id=comment_id,
    )
    # The comment's FK guarantees its bug exists, so use it directly.
    bug = db.get(Bug, bug_id)
    _notify_item_stakeholders(
        db, bug, actor, kind="comment", background=background,
        title=f"Comment deleted on #{bug_id}",
        body=f"{actor.name} deleted a comment by {author_name} on “{bug.title}”.",
    )
    db.commit()
    return {"message": "Comment deleted"}


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------
@router.get("/{bug_id}/activity", response_model=list[ActivityOut])
def list_activity(
    bug_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[Activity]:
    if db.get(Bug, bug_id) is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    return list(db.scalars(
        select(Activity).where(Activity.bug_id == bug_id)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
        .limit(_DETAIL_ACTIVITIES_MAX)
    ).all())


# ---------------------------------------------------------------------------
# Item links
# ---------------------------------------------------------------------------
@router.get("/{bug_id}/links", response_model=list[BugLinkOut])
def list_links(
    bug_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    if db.get(Bug, bug_id) is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    return _bug_links(db, bug_id)


def _reload_link(db: Session, link_id: int) -> BugLink:
    link = db.scalar(
        select(BugLink)
        .options(selectinload(BugLink.source), selectinload(BugLink.target))
        .where(BugLink.id == link_id)
    )
    if link is None:
        # Concurrently deleted between insert/find and this reload; return a
        # clean 409 instead of AttributeError-ing in _serialize_link.
        raise HTTPException(status_code=409,
                            detail="Link was modified concurrently. Please retry.")
    return link


def _insert_link_or_existing(db: Session, link: BugLink, refetch) -> tuple[BugLink, bool]:
    """Flush a new BugLink; on a unique-index race, roll back and return the
    existing edge. Returns ``(edge, created)``.

    Keeps the "re-linking returns the existing edge" contract atomic: a
    concurrent identical request that wins the race raises IntegrityError here,
    converted into the existing edge instead of a 500. ``refetch`` re-runs the
    existence lookup after the rollback.
    """
    db.add(link)
    try:
        db.flush()
        return link, True
    except IntegrityError:
        db.rollback()
        existing = refetch()
        if existing is None:
            raise
        return existing, False


def _link_stakeholders(bug: Bug) -> list[int | None]:
    """Reporter + assignee ids for one end of a link."""
    return [bug.reporter_id, *[a.id for a in bug.assignees]]


def _notify_link_endpoints(
    db: Session, recipients: list[int | None], primary_id: int, actor: User, *,
    background: "BackgroundTasks | None", title: str, body: str,
) -> None:
    """Notify the (already-collected) reporters + assignees of both ends of a
    link, since a link is a change to both items. ``notify()`` dedupes ids and
    drops the actor, so a user on both ends gets a single bell entry, and the
    same row feeds the email digest. Deep-links to ``primary_id`` (the item the
    request was on)."""
    notification_service.notify(
        db, recipients, kind="updated", background=background,
        title=title, body=body, bug_id=primary_id,
        actor_name=actor.name, exclude=actor.id,
    )


@router.post("/{bug_id}/links", response_model=BugLinkOut, status_code=status.HTTP_201_CREATED)
def add_link(
    bug_id: int,
    payload: BugLinkIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    """Create a directed link from this bug to another. Requires edit rights on
    the source item (linking changes that item's relationships). Idempotent on
    (source, target, type): re-linking returns the existing edge."""
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    _validate_update_authorization(bug, actor)
    if payload.target_bug_id == bug_id:
        raise HTTPException(status_code=400, detail="An item can't be linked to itself")
    target = db.scalar(_eager_bug().where(Bug.id == payload.target_bug_id))
    if target is None:
        raise HTTPException(status_code=400, detail="Target item does not exist")
    _require_can_edit_link_endpoint(target, actor)
    _reject_inverse_directional_link(db, bug_id, target, payload.link_type)

    # 'relates' is symmetric, so A->B and B->A are the same relationship. The
    # unique index is direction-specific, so without this a reverse-relates
    # would create a duplicate row; return the existing reverse edge idempotently
    # instead (mirrors the same-direction idempotency below).
    if payload.link_type == "relates":
        reverse = db.scalar(
            select(BugLink).where(
                BugLink.source_bug_id == target.id,
                BugLink.target_bug_id == bug_id,
                BugLink.link_type == "relates",
            )
        )
        if reverse is not None:
            return _serialize_link(_reload_link(db, reverse.id), bug_id)

    def _find_existing() -> Optional[BugLink]:
        return db.scalar(
            select(BugLink).where(
                BugLink.source_bug_id == bug_id,
                BugLink.target_bug_id == target.id,
                BugLink.link_type == payload.link_type,
            )
        )

    existing = _find_existing()
    if existing is not None:
        return _serialize_link(_reload_link(db, existing.id), bug_id)

    link, created = _insert_link_or_existing(
        db,
        BugLink(
            source_bug_id=bug_id, target_bug_id=target.id,
            link_type=payload.link_type, created_by_user_id=actor.id,
        ),
        _find_existing,
    )
    if not created:
        # A concurrent identical request won the unique-index race; return the
        # existing edge idempotently instead of 500ing.
        return _serialize_link(_reload_link(db, link.id), bug_id)
    _log(
        db, bug_id, actor, "link_added",
        f"#{bug_id} '{bug.title}' {_link_phrase(payload.link_type, 'outgoing')} "
        f"#{target.id} '{target.title}'",
    )
    _notify_link_endpoints(
        db, _link_stakeholders(bug) + _link_stakeholders(target), bug.id, actor,
        background=background,
        title=f"Link added on #{bug_id}",
        body=(
            f"{actor.name} linked “{bug.title}” "
            f"{_link_phrase(payload.link_type, 'outgoing')} "
            f"#{target.id} “{target.title}”."
        ),
    )
    db.commit()
    return _serialize_link(_reload_link(db, link.id), bug_id)


@router.delete("/{bug_id}/links/{link_id}")
def remove_link(
    bug_id: int, link_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict[str, str]:
    """Remove a link. The path bug must be one of the two endpoints, and the
    caller must be able to edit it."""
    link = db.get(BugLink, link_id)
    if link is None or bug_id not in (link.source_bug_id, link.target_bug_id):
        raise HTTPException(status_code=404, detail="Link not found")
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    _validate_update_authorization(bug, actor)
    other_id = link.target_bug_id if link.source_bug_id == bug_id else link.source_bug_id
    other = db.scalar(_eager_bug().where(Bug.id == other_id))
    # Build the notify recipients inside the existing endpoint guard so both
    # ends are covered without a second None branch.
    recipients = _link_stakeholders(bug)
    if other is not None:
        _require_can_edit_link_endpoint(other, actor)
        recipients += _link_stakeholders(other)
    db.delete(link)
    _log(db, bug_id, actor, "link_removed", f"Unlinked #{bug_id} ↔ #{other_id}")
    _notify_link_endpoints(
        db, recipients, bug.id, actor, background=background,
        title=f"Link removed on #{bug_id}",
        body=f"{actor.name} unlinked “{bug.title}” from #{other_id}.",
    )
    db.commit()
    return {"message": "Link removed"}


# ---------------------------------------------------------------------------
# Bulk actions: one toolbar request mutates many selected items, each through
# the same permission, audit, and notification path as its single-item
# endpoint. Items the caller can't action are skipped rather than erroring, so
# a mixed selection partially succeeds and the response reports the tally.
# ---------------------------------------------------------------------------
def _norm_or_400(value: Optional[str], allowed: list[str], label: str) -> str:
    try:
        return normalize_choice(value or "", allowed, label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _bulk_resolve_value(payload: BulkActionIn):
    """Validate the action's value once up front so a bad value is a single 400
    for the whole request. Returns the canonical status/priority/env string, or
    None for delete."""
    action = payload.action
    if action == "set_status":
        return _norm_or_400(payload.value, ALLOWED_STATUSES, "status")
    if action == "set_priority":
        return _norm_or_400(payload.value, ALLOWED_PRIORITIES, "priority")
    if action == "set_environment":
        return _norm_or_400(payload.value, ALLOWED_ENVIRONMENTS, "environment")
    return None  # delete


def _bulk_notify_update(db: Session, bug: Bug, actor: User, what: str,
                        background: BackgroundTasks) -> None:
    _itype = _item_type(bug).lower()
    notification_service.notify(
        db, [bug.reporter_id, *[a.id for a in bug.assignees]],
        kind="updated", background=background,
        title=f"{_itype.capitalize()} #{bug.id} updated",
        body=f"{actor.name} changed {what} (bulk).",
        bug_id=bug.id, actor_name=actor.name, exclude=actor.id,
    )


def _bulk_set_status(db: Session, bug: Bug, actor: User, value: str,
                     background: BackgroundTasks) -> str:
    # Per-type validity: a status legal for a Bug may be illegal for a Task.
    if value not in statuses_for_type(_item_type(bug)):
        return "skipped"
    if bug.status == value:
        return "skipped"
    old = bug.status
    bug.status = value
    # Bump the optimistic-concurrency version so a client holding a stale copy
    # detects the bulk change (mirrors the single-item _persist_update);
    # otherwise a later expected_version PUT would clobber the bulk change.
    bug.version = (bug.version or 1) + 1
    _log(db, bug.id, actor, "status_changed",
         f"#{bug.id} '{bug.title}' — status: '{old}' → '{value}' (bulk)")
    _bulk_notify_update(db, bug, actor, "status", background)
    return "updated"


def _bulk_set_field(db: Session, bug: Bug, actor: User, field_name: str,
                    value: str, background: BackgroundTasks) -> str:
    old = getattr(bug, field_name)
    if old == value:
        return "skipped"
    setattr(bug, field_name, value)
    # Bump version so the bulk change is visible to version-tracking clients
    # (see _bulk_set_status).
    bug.version = (bug.version or 1) + 1
    _log(db, bug.id, actor, f"{field_name}_changed",
         f"#{bug.id} '{bug.title}' — {field_name}: '{old}' → '{value}' (bulk)")
    _bulk_notify_update(db, bug, actor, field_name, background)
    return "updated"


def _bulk_delete(db: Session, bug: Bug, actor: User, background: BackgroundTasks) -> str:
    # Mirror delete_bug: notify the reporter + assignees before the row is gone
    # (link_bug=False, since the bug_id FK cascades and the notification can't
    # deep-link the about-to-vanish item), then detach audit history
    # (bug_id -> NULL) so the trail survives, delete the row, and record the
    # delete. The notify row also feeds the email digest, so a bulk delete
    # reaches assignees on every channel like a single delete.
    title, itype, bug_id = bug.title, _item_type(bug), bug.id
    _notify_item_stakeholders(
        db, bug, actor, kind="updated", background=background, link_bug=False,
        title=f"{itype} #{bug_id} deleted",
        body=f"{actor.name} deleted “{title}”.",
    )
    db.execute(update(Activity).where(Activity.bug_id == bug_id).values(bug_id=None))
    db.flush()
    db.delete(bug)
    db.add(Activity(
        bug_id=None, entity_type="bug", entity_id=bug_id,
        actor_user_id=actor.id, actor_name=actor.name, action="bug_deleted",
        detail=f"Deleted {itype.lower()} #{bug_id}: {title} (bulk)",
    ))
    return "updated"


def _bulk_version_conflict(bug: Bug, payload: BulkActionIn) -> bool:
    """True when the caller supplied an expected version for this bug and it
    drifted, so the bulk op must skip it rather than clobber a concurrent edit."""
    if payload.expected_versions is None:
        return False
    expected = payload.expected_versions.get(bug.id)
    return expected is not None and bug.version != expected


def _apply_bulk_to_bug(db: Session, bug: Bug, actor: User, payload: BulkActionIn,
                       resolved, background: BackgroundTasks) -> str:
    """Apply one bulk action to one bug. Returns 'updated' / 'skipped' /
    'conflict'. Permission failures are 'skipped' so a mixed selection partially
    succeeds; a version drift is a 'conflict'."""
    if _bulk_version_conflict(bug, payload):
        return "conflict"
    action = payload.action
    if action == "delete":
        if not can_delete_bug(actor, item_type=_item_type(bug)):
            return "skipped"
        return _bulk_delete(db, bug, actor, background)
    if not can_edit_bug(actor, bug.reporter_id, [a.id for a in bug.assignees],
                        item_type=_item_type(bug)):
        return "skipped"
    if action == "set_status":
        return _bulk_set_status(db, bug, actor, resolved, background)
    if action == "set_priority":
        return _bulk_set_field(db, bug, actor, "priority", resolved, background)
    return _bulk_set_field(db, bug, actor, "environment", resolved, background)


@router.post("/bulk", response_model=BulkActionResult)
def bulk_action(
    payload: BulkActionIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> BulkActionResult:
    """Apply one action to many selected items at once. Each item is gated by
    the same permission rules as its single-item endpoint; items the caller
    can't touch are skipped. Returns the updated / skipped / not-found tally."""
    resolved = _bulk_resolve_value(payload)
    updated = skipped = failed = conflicts = 0
    # Batch-load every selected item in one eager query (selectinload batches
    # the relationship loads across all of them) instead of a fresh eager query
    # per id. Iterate the caller's id order against the map so the not-found
    # tally stays accurate.
    by_id = {
        b.id: b
        for b in db.scalars(_eager_bug().where(Bug.id.in_(payload.ids))).all()
    }
    for bug_id in payload.ids:
        bug = by_id.get(bug_id)
        if bug is None:
            failed += 1
            continue
        outcome = _apply_bulk_to_bug(db, bug, actor, payload, resolved, background)
        if outcome == "updated":
            updated += 1
        elif outcome == "conflict":
            conflicts += 1
        else:
            skipped += 1
    db.commit()
    parts = [f"{updated} updated"]
    if conflicts:
        parts.append(f"{conflicts} changed since you loaded them")
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        parts.append(f"{failed} not found")
    return BulkActionResult(
        updated=updated, skipped=skipped, failed=failed, conflicts=conflicts,
        message=", ".join(parts),
    )
