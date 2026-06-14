"""Bugs API + comments + attachments + activity (per-bug)."""
from __future__ import annotations

import io
import re
import threading
import time
from collections import deque
from typing import Optional
from urllib.parse import quote

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException,
    Query, UploadFile, status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.auth import can_delete_bug, can_edit_bug, get_current_user
from app.database import get_db
from app.email_service import (
    BugSnapshot, UserSnapshot,
    notify_assignment, notify_bug_created, notify_bug_updated, notify_comment_added,
)
from app import notification_service
from app.image_strip import strip_image_metadata
from app.models import Activity, Attachment, Bug, Comment, Event, Project, User
from app.schemas import (
    ALLOWED_ENVIRONMENTS, ALLOWED_ITEM_TYPES, ALLOWED_PRIORITIES,
    ALLOWED_STATUSES, ActivityOut, AttachmentBrief, BugCreate, BugDetail,
    BugListResponse, BugOut, BugUpdate, CommentIn, CommentOut,
    normalize_choice, statuses_for_type,
)

router = APIRouter(prefix="/api/bugs", tags=["bugs"])

# Repeated HTTPException detail strings — extracted so Sonar's S1192
# duplicate-string-literal rule stays quiet and so the wording stays
# consistent across endpoints.
_DETAIL_BUG_NOT_FOUND = "Bug not found"
_DEFAULT_MIME = "application/octet-stream"

# Soft cap on individual attachment size — protects the DB from a 4 GB video
# upload. Configurable via env if the team needs bigger files later.
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

# Read uploads in 1 MB chunks so we abort over-sized requests before
# they consume RAM. Anything above MAX_FILE_BYTES is rejected mid-stream.
_UPLOAD_CHUNK = 1024 * 1024

# Per-user rate limit on attachment uploads (v3.2.1). Without this, an
# authenticated user (or a stolen session) can chain 50 MB POSTs and
# bloat the DB by tens of GB in minutes. 20/min is generous for humans
# attaching screenshots and tight enough to make automated abuse obvious.
_UPLOAD_RATE_WINDOW_SECONDS = 60
_UPLOAD_RATE_MAX = 20
_upload_buckets: dict[int, deque] = {}
_upload_rate_lock = threading.Lock()
# Bound the dict so a churn of test users doesn't grow memory forever.
_UPLOAD_BUCKETS_MAX = 5_000

# Per-user comment rate limit (v3.0). Each comment writes a row AND fans out
# notifications + emails to the reporter and every assignee, so unbounded
# commenting is a notification/email-amplification vector. 30/min is generous
# for a human discussion and tight enough to make a script obvious.
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

# Content types we MUST NOT serve as-is, because a browser would render
# them inline and execute embedded scripts in our same-origin context.
# These get downgraded to application/octet-stream at download time and
# served with Content-Disposition: attachment to force the browser to
# save them rather than render them.
_ACTIVE_CONTENT_TYPES = {
    "text/html", "application/xhtml+xml", "application/xml", "text/xml",
    "image/svg+xml", "application/javascript", "text/javascript",
    "application/x-javascript", "text/javascript;charset=utf-8",
}

# MIME types a browser may render inline without executing anything.
# Everything outside this safelist is forced to Content-Disposition:
# attachment — a blocklist (_ACTIVE_CONTENT_TYPES) alone can't keep up
# with every renderable type a browser might treat actively. SVG is
# deliberately absent (scriptable); it's also in the blocklist above so
# it additionally gets the octet-stream treatment.
_INLINE_SAFE_PREFIXES = ("image/", "video/", "audio/")
_INLINE_SAFE_TYPES = {"application/pdf", "text/plain", "text/csv"}

# Sanitize filename when echoed in headers — we still keep the original
# in the DB; this is purely the bytes that go into Content-Disposition.
# HTTP header values must be ASCII (RFC 7230); RFC 6266 says the plain
# `filename=` parameter is US-ASCII only, with `filename*=` carrying any
# non-ASCII form via percent-encoding. We enforce ASCII here.
_HEADER_FILENAME_BAD = re.compile(r'[\r\n"\\]+')


def _safe_filename_for_header(name: str) -> str:
    """Return an ASCII-only, header-safe version of the filename.

    Strips CR/LF/quotes/backslashes that would break the
    Content-Disposition header, and replaces any non-ASCII byte with an
    underscore. The original (possibly-Unicode) form is still preserved
    on the wire via the RFC 5987 ``filename*=`` parameter the caller
    appends — see ``download_attachment``. Without this ASCII pass, a
    non-ASCII filename would be Latin-1 encoded by the HTTP layer and
    arrive at the client as garbage bytes (or, with strict clients,
    reject the response outright).
    """
    cleaned = _HEADER_FILENAME_BAD.sub("_", name)
    # Replace anything outside printable ASCII with `_`.
    ascii_only = "".join(c if 32 <= ord(c) < 127 else "_" for c in cleaned)
    return ascii_only or "file"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _item_type(bug: Bug) -> str:
    """Work-item flavour ('Bug' / 'Requirement' / 'Task'), defaulting legacy
    rows created before the item_type column existed to 'Bug'."""
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
        # Default "Bug" lets legacy bugs (predating the column) still
        # render correct-flavored emails without raising AttributeError.
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
    literal characters, not 'any character' / 'any sequence'. We pair this
    with `escape='\\\\'` on the LIKE clause so the engine knows about it."""
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
def _normalize_choice_list(values: Optional[list[str]], allowed: list[str], label: str) -> list[str]:
    """Normalize a multi-valued enum query param. Strip empties; reject
    unknown values with 400 (same behavior as the legacy single-value path)."""
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
    if q_clean.isdigit():
        return _apply_where_both(stmt, count_stmt, Bug.id == int(q_clean))
    if not q_clean:
        return stmt, count_stmt
    # Use the cleaned query — old code used the un-stripped `q`, which made
    # `?q=  needle  ` never match anything because the LIKE pattern itself
    # contained the leading/trailing spaces.
    like = f"%{_like_escape(q_clean.lower())}%"
    clause = or_(
        func.lower(Bug.title).like(like, escape="\\"),
        func.lower(Bug.description).like(like, escape="\\"),
    )
    return _apply_where_both(stmt, count_stmt, clause)


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
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> BugListResponse:
    """List bugs with filtering. All enum-like filters accept MULTIPLE values
    via repeated query params (?status=New&status=Resolved). Single-value
    calls (?status=New) still work — FastAPI parses into a list of one which
    we then `.in_(...)` against."""
    if page < 1 or page_size < 1 or page_size > 200:
        raise HTTPException(status_code=400, detail="Invalid pagination parameters")

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
    stmt = stmt.order_by(Bug.updated_at.desc(), Bug.id.desc()).limit(page_size).offset(offset)
    bugs = list(db.scalars(stmt).all())

    # Perf: previously this loop called `_attachment_count(db, b.id)` once per
    # bug, which is N+1 queries (50 extra round-trips for a single page on a
    # low-resource VM). Replaced with one aggregate query keyed by bug_id.
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
@router.get("/{bug_id}", response_model=BugDetail)
def get_bug(
    bug_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BugDetail:
    bug = db.scalar(
        _eager_bug().options(
            selectinload(Bug.comments),
            selectinload(Bug.activities),
        ).where(Bug.id == bug_id)
    )
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)

    # Pull all attachments (bug-level + comment-level), grouped per-comment.
    # v2.6: newest attachment first so the most recent evidence is at
    # the top of each bucket on the modal.
    all_atts = list(db.scalars(
        select(Attachment).where(Attachment.bug_id == bug_id)
        .order_by(Attachment.created_at.desc(), Attachment.id.desc())
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
        len(all_atts),
        can_edit_bug(user, bug.reporter_id, [a.id for a in bug.assignees],
                     item_type=_item_type(bug)),
    )
    payload["attachments"] = [_attachment_brief(a) for a in bug_level]
    payload["comments"] = []
    for c in bug.comments:
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
        for a in bug.activities
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
    # v3.0 in-app notifications — same recipients the emails target. Written on
    # this session so they commit transactionally with the bug.
    _itype = _item_type(bug).lower()
    assignee_ids = [a.id for a in assignees]
    notification_service.notify(
        db, assignee_ids, kind="assigned", background=background,
        title=f"Assigned to {_itype} #{bug.id}",
        body=f"{actor.name} assigned you to “{bug.title}”.",
        bug_id=bug.id, actor_name=actor.name, exclude=actor.id,
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
        # v2.3: regular users can no longer edit Tasks or Requirements.
        noun = _item_type(bug).lower()
        raise HTTPException(
            status_code=403,
            detail=f"You don't have permission to edit this {noun}.",
        )


def _normalize_update_event_id(fields: dict, db: Session) -> None:
    if "event_id" in fields and fields["event_id"]:
        if db.get(Event, fields["event_id"]) is None:
            raise HTTPException(status_code=400, detail="Event does not exist")
    if "event_id" in fields and fields["event_id"] == 0:
        # Treat 0 as "unlink" for clients that can't easily send JSON null.
        fields["event_id"] = None


def _validate_update_status(fields: dict, bug: Bug) -> None:
    """v2.5: per-type status validation. Pydantic only checks the union;
    here we check the per-type set against the (possibly changing) type."""
    if "status" not in fields or fields["status"] is None:
        return
    effective_type = fields.get("item_type") or _item_type(bug)
    allowed_for_type = statuses_for_type(effective_type)
    if fields["status"] not in allowed_for_type and fields["status"] != bug.status:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Status '{fields['status']}' is not valid for "
                f"{effective_type}. Allowed: {', '.join(allowed_for_type)}"
            ),
        )


def _validate_update_payload(fields: dict, bug: Bug, db: Session) -> None:
    if "project_id" in fields and fields["project_id"] is not None:
        if db.get(Project, fields["project_id"]) is None:
            raise HTTPException(status_code=400, detail="Project does not exist")
    _normalize_update_event_id(fields, db)
    _validate_update_status(fields, bug)


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
        bug.reporter_id = new_reporter.id
        new_reporter_label = new_reporter.name if new_reporter else "—"
    if old_reporter_label != new_reporter_label:
        changes.append(("reporter", old_reporter_label, new_reporter_label))


def _apply_assignee_diff(bug: Bug, db: Session, assignee_ids: Optional[list[int]],
                        changes: list[tuple[str, str, str]]) -> list[User]:
    """Diff and re-bind assignees if the set actually changed. Returns the
    list of NEWLY-added users so the caller can notify them."""
    if assignee_ids is None:
        return []
    new_users = _resolve_users(db, assignee_ids)
    old_ids = {a.id for a in bug.assignees}
    new_ids = {u.id for u in new_users}
    added_ids = new_ids - old_ids
    removed_ids = old_ids - new_ids
    if not (added_ids or removed_ids):
        return []
    old_names = sorted(a.name for a in bug.assignees)
    new_names = sorted(u.name for u in new_users)
    changes.append((
        "assignees",
        ", ".join(old_names) or "(none)",
        ", ".join(new_names) or "(none)",
    ))
    bug.assignees = new_users  # only re-bind when actually different
    return [u for u in new_users if u.id in added_ids]


def _persist_update(db: Session, bug: Bug, actor: User,
                    changes: list[tuple[str, str, str]]) -> None:
    """Commit when there are tracked changes; rollback otherwise so a no-op
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
    db.commit()


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
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    _validate_update_authorization(bug, actor)

    fields = payload.model_dump(exclude_unset=True)
    _validate_update_payload(fields, bug, db)

    assignee_ids = fields.pop("assignee_ids", None)
    has_reporter_in_payload = "reporter_id" in fields
    new_reporter_id = fields.pop("reporter_id", None)

    # BUG-2 fix: only run the reporter-change gate when it actually CHANGES.
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
    newly_assigned = _apply_assignee_diff(bug, db, assignee_ids, changes)

    _persist_update(db, bug, actor, changes)

    # v3.0 in-app notifications mirror the update emails. Newly-assigned users
    # get a single "assigned" notification (not also "updated"), so each
    # recipient gets exactly one per change set.
    if changes or newly_assigned:
        new_ids = {u.id for u in newly_assigned}
        _itype = _item_type(bug).lower()
        if changes:
            recipients = [
                uid for uid in [bug.reporter_id, *[a.id for a in bug.assignees]]
                if uid not in new_ids
            ]
            notification_service.notify(
                db, recipients, kind="updated", background=background,
                title=f"{_itype.capitalize()} #{bug.id} updated",
                body=f"{actor.name} changed " + ", ".join(f for f, _, _ in changes) + ".",
                bug_id=bug.id, actor_name=actor.name, exclude=actor.id,
            )
        if newly_assigned:
            notification_service.notify(
                db, list(new_ids), kind="assigned", background=background,
                title=f"Assigned to {_itype} #{bug.id}",
                body=f"{actor.name} assigned you to “{bug.title}”.",
                bug_id=bug.id, actor_name=actor.name, exclude=actor.id,
            )
        db.commit()

    fresh = db.scalar(_eager_bug().where(Bug.id == bug_id))
    snap = _bug_snapshot(fresh)
    _schedule_update_notifications(background, snap, changes, newly_assigned, actor)

    return BugOut.model_validate(_bug_to_out_dict(
        fresh, _attachment_count(db, bug_id),
        can_edit_bug(actor, fresh.reporter_id, [a.id for a in fresh.assignees],
                     item_type=fresh.item_type),
    ))


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
@router.delete("/{bug_id}")
def delete_bug(
    bug_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict[str, str]:
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    # v3.1 spec: item deletion is admin-only across every type — managers
    # can edit, never delete. Reporters and assignees never could.
    if not can_delete_bug(actor, item_type=_item_type(bug)):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete items.",
        )
    title = bug.title
    itype = _item_type(bug)
    # Detach the bug's audit history BEFORE deleting the bug so the trail
    # survives. Two-step on purpose:
    #   1. UPDATE activity_log SET bug_id = NULL WHERE bug_id = <id>
    #   2. DELETE FROM bugs WHERE id = <id>
    # This works on both the new schema (ondelete="SET NULL") and any older
    # production DB that still has the original ondelete="CASCADE" — by the
    # time the DELETE fires, no activity row references this bug, so the
    # legacy cascade has nothing to cascade onto. The rows keep entity_id
    # pointing at the now-gone bug, and their `detail` field still carries
    # the original title, so searching the global audit screen still works.
    db.execute(
        update(Activity)
        .where(Activity.bug_id == bug_id)
        .values(bug_id=None)
    )
    db.flush()
    db.delete(bug)
    # Add one summary row so the trail explicitly records the delete itself.
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
    # v2.6: newest comments first (matches Bug.comments relationship
    # ordering used by the detail endpoint).
    comments = list(db.scalars(
        select(Comment).where(Comment.bug_id == bug_id)
        .order_by(Comment.created_at.desc(), Comment.id.desc())
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
    _check_comment_rate(author.id)
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)

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
    # v3.0 in-app notification — reporter + assignees, minus the author.
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
    """Stream the upload in chunks and abort EARLY if it exceeds the limit.
    Replaces the prior `await file.read()` which buffered the entire body
    in memory before checking — letting an attacker waste GBs of RAM with
    a single oversized request."""
    buf = bytearray()
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max {limit // (1024 * 1024)} MB.",
            )
    return bytes(buf)


@router.post("/{bug_id}/attachments", response_model=AttachmentBrief, status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    bug_id: int,
    file: UploadFile = File(...),
    comment_id: Optional[int] = Form(default=None),
    db: Session = Depends(get_db),
    uploader: User = Depends(get_current_user),
) -> dict:
    # Per-user rate limit. Authenticated, yes — but the upload endpoint
    # writes a 50 MB BLOB per call, so a hostile client can bloat the DB
    # quickly without this guard. Raised BEFORE we touch the multipart body.
    _check_upload_rate(uploader.id)

    bug = db.get(Bug, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    if comment_id is not None:
        c = db.get(Comment, comment_id)
        if c is None or c.bug_id != bug_id:
            raise HTTPException(status_code=400, detail="Invalid comment_id for this bug")

    data = await _read_upload_with_limit(file, MAX_FILE_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    # T6: strip EXIF / GPS / camera-serial / XMP / ICC from raster image
    # uploads. No-op for non-images and fail-open on errors so an exotic
    # image format never blocks the upload.
    data = strip_image_metadata(data, file.content_type)

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
    db.commit()
    db.refresh(att)
    return _attachment_brief(att)


@router.get("/{bug_id}/attachments/{att_id}/download")
def download_attachment(
    bug_id: int, att_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    a = db.get(Attachment, att_id)
    if a is None or a.bug_id != bug_id:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Decide content-type and disposition.
    #
    # Active types like text/html, image/svg+xml, JS, etc. can carry
    # executable script. If we let the browser render them inline they'll
    # run in our origin's context — same-origin XSS via stored attachment.
    # For those types we force `attachment` disposition AND downgrade the
    # content-type to octet-stream so the browser saves rather than executes.
    ct_lower = (a.content_type or "").lower().split(";")[0].strip()
    is_active = ct_lower in _ACTIVE_CONTENT_TYPES
    safe_ct = _DEFAULT_MIME if is_active else (a.content_type or _DEFAULT_MIME)
    inline_ok = not is_active and (
        ct_lower in _INLINE_SAFE_TYPES
        or ct_lower.startswith(_INLINE_SAFE_PREFIXES)
    )
    disposition = "inline" if inline_ok else "attachment"

    safe_fname = _safe_filename_for_header(a.filename)
    # RFC 5987 form for non-ASCII filenames; keeps a plain ASCII fallback.
    cd = (
        f'{disposition}; filename="{safe_fname}"; '
        f"filename*=UTF-8''{quote(a.filename, safe='')}"
    )

    return StreamingResponse(
        io.BytesIO(a.data),
        media_type=safe_ct,
        headers={
            "Content-Disposition": cd,
            "Content-Length": str(a.size_bytes),
            # Defense-in-depth: even if some future code path ends up
            # serving HTML inline, these headers make it harder to weaponize.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Frame-Options": "DENY",
            # Keep a Cache-Control here so the global middleware doesn't
            # try to override us — attachments may be private.
            "Cache-Control": "private, max-age=0, no-cache",
        },
    )


@router.delete("/{bug_id}/attachments/{att_id}")
def delete_attachment(
    bug_id: int, att_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    a = db.get(Attachment, att_id)
    if a is None or a.bug_id != bug_id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    # v2.5: attachment deletion is admin-only across the board (both
    # bug-level and comment-level). The product spec is "Comments and
    # Attachments must not be editable or deletable by anyone except the
    # admin." Uploaders and managers can no longer remove their own files
    # — admins curate the evidence.
    if actor.role != "admin":
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
    db.commit()
    return {"message": "Attachment deleted"}


# ---------------------------------------------------------------------------
# Comment edit / delete — admin only (v2.5)
# ---------------------------------------------------------------------------
@router.put("/{bug_id}/comments/{comment_id}", response_model=CommentOut)
def update_comment(
    bug_id: int, comment_id: int,
    payload: CommentIn,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    """Edit a comment's body. Admin only — see route-module note. The
    audit trail records who edited and what the new body was so an
    accidental rewrite is traceable."""
    if actor.role != "admin":
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
    db.commit()
    db.refresh(c)
    atts = list(db.scalars(
        select(Attachment).where(Attachment.comment_id == c.id)
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
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    """Delete a comment AND its attachments (the FK on attachments
    cascades from comment_id). Admin only. The audit row keeps the
    deleted body's preview so a moderator review still has context."""
    if actor.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete comments.",
        )
    c = db.get(Comment, comment_id)
    if c is None or c.bug_id != bug_id:
        raise HTTPException(status_code=404, detail="Comment not found")
    preview = (c.body or "")[:80]
    db.delete(c)
    _log(
        db, bug_id, actor, "comment_deleted",
        f"Comment #{comment_id} by {c.author_name} deleted: {preview}",
        entity_type="comment", entity_id=comment_id,
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
    ).all())
