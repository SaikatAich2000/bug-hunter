"""Bugs API + comments + attachments + activity (per-bug)."""
from __future__ import annotations

import csv
import io
import re
import threading
import time
from collections import deque
from typing import Optional
from urllib.parse import quote

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException,
    Query, Response, UploadFile, status,
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
from app.models import Activity, Attachment, Bug, Comment, Event, Project, User
from app.schemas import (
    ALLOWED_ENVIRONMENTS, ALLOWED_ITEM_TYPES, ALLOWED_PRIORITIES,
    ALLOWED_STATUSES, ActivityOut, AttachmentBrief, BugCreate, BugDetail,
    BugListResponse, BugOut, BugUpdate, CommentIn, CommentOut,
    normalize_choice,
)

router = APIRouter(prefix="/api/bugs", tags=["bugs"])

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


def _check_upload_rate(user_id: int) -> None:
    """Raise 429 if the user is uploading too fast.

    Sliding window of timestamps per user. Cheap, no Redis. Multi-worker
    deployments get per-worker buckets — for tighter global limits put
    nginx limit_req in front of the upload endpoints.
    """
    now = time.monotonic()
    cutoff = now - _UPLOAD_RATE_WINDOW_SECONDS
    with _upload_rate_lock:
        bucket = _upload_buckets.get(user_id)
        if bucket is None:
            if len(_upload_buckets) >= _UPLOAD_BUCKETS_MAX:
                _upload_buckets.pop(next(iter(_upload_buckets)), None)
            bucket = deque()
            _upload_buckets[user_id] = bucket
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= _UPLOAD_RATE_MAX:
            retry_after = max(1, int(_UPLOAD_RATE_WINDOW_SECONDS - (now - bucket[0])))
            raise HTTPException(
                status_code=429,
                detail="Too many uploads, slow down a moment.",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)

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
        "item_type": getattr(bug, "item_type", None) or "Bug",
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
        item_type=getattr(bug, "item_type", None) or "Bug",
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


def _eager_bug(db: Session) -> "select":
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
# CSV export — must come before /{bug_id}
# ---------------------------------------------------------------------------
@router.get("/export.csv")
def export_bugs_csv(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> Response:
    rows = db.scalars(_eager_bug(db).order_by(Bug.id.asc())).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "type", "project", "title", "status", "priority", "environment",
        "reporter_name", "reporter_email", "assignees", "due_date",
        "created_at", "updated_at", "description",
    ])
    for b in rows:
        writer.writerow([
            b.id,
            getattr(b, "item_type", None) or "Bug",
            b.project.name if b.project else "",
            b.title, b.status, b.priority, b.environment,
            b.reporter.name if b.reporter else "",
            b.reporter.email if b.reporter else "",
            "; ".join(f"{a.name} <{a.email}>" for a in b.assignees),
            b.due_date or "",
            b.created_at.isoformat(),
            b.updated_at.isoformat(),
            b.description.replace("\n", " ").replace("\r", " "),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="bugs.csv"'},
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
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
    """List bugs with filtering. All enum-like filters now accept MULTIPLE
    values via repeated query params (e.g. ?status=New&status=Resolved) so
    the SPA's multi-select dropdowns can pass them through directly. Single-
    value calls (?status=New) still work — FastAPI parses them into a list
    of one, which we then `.in_(...)` against."""
    if page < 1 or page_size < 1 or page_size > 200:
        raise HTTPException(status_code=400, detail="Invalid pagination parameters")

    # Normalize each multi-valued enum filter case-insensitively. We strip
    # empty strings (the SPA sometimes sends ?status= for "no filter") and
    # reject unknown values with 400 — same behavior as the old single-value
    # path, just per-element.
    def _normalize_list(values: Optional[list[str]], allowed: list[str], label: str) -> list[str]:
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

    statuses = _normalize_list(status_filter, ALLOWED_STATUSES, "status")
    priorities = _normalize_list(priority, ALLOWED_PRIORITIES, "priority")
    environments = _normalize_list(environment, ALLOWED_ENVIRONMENTS, "environment")
    item_types = _normalize_list(item_type, ALLOWED_ITEM_TYPES, "item_type")

    # Strip None / 0 from the int lists so callers can send blanks safely.
    project_ids = [p for p in (project_id or []) if p]
    assignee_ids = [a for a in (assignee_id or []) if a]

    stmt = _eager_bug(db)
    count_stmt = select(func.count(Bug.id))

    def apply(both, clause):
        return both[0].where(clause), both[1].where(clause)

    if project_ids:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.project_id.in_(project_ids))
    if statuses:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.status.in_(statuses))
    if priorities:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.priority.in_(priorities))
    if environments:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.environment.in_(environments))
    if item_types:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.item_type.in_(item_types))
    if reporter_id is not None:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.reporter_id == reporter_id)
    if assignee_ids:
        stmt, count_stmt = apply(
            (stmt, count_stmt),
            Bug.assignees.any(User.id.in_(assignee_ids)),
        )
    if due_date:
        # Exact-day match. Format is YYYY-MM-DD; we don't validate it
        # strictly here — a malformed value just won't match.
        stmt, count_stmt = apply((stmt, count_stmt), Bug.due_date == due_date)
    if event_id is not None:
        # event_id=0 means "not in any event" — distinct from "any event".
        if event_id == 0:
            stmt, count_stmt = apply((stmt, count_stmt), Bug.event_id.is_(None))
        else:
            stmt, count_stmt = apply((stmt, count_stmt), Bug.event_id == event_id)
    if q:
        q_clean = q.strip().lstrip("#")
        if q_clean.isdigit():
            stmt, count_stmt = apply((stmt, count_stmt), Bug.id == int(q_clean))
        elif q_clean:
            # Use the cleaned query — old code used the un-stripped `q` here,
            # which made `?q=  needle  ` never match anything because the
            # LIKE pattern itself contained the leading/trailing spaces.
            like = f"%{_like_escape(q_clean.lower())}%"
            clause = or_(
                func.lower(Bug.title).like(like, escape="\\"),
                func.lower(Bug.description).like(like, escape="\\"),
            )
            stmt, count_stmt = apply((stmt, count_stmt), clause)

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
                         item_type=getattr(b, "item_type", None) or "Bug"),
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
        _eager_bug(db).options(
            selectinload(Bug.comments),
            selectinload(Bug.activities),
        ).where(Bug.id == bug_id)
    )
    if bug is None:
        raise HTTPException(status_code=404, detail="Bug not found")

    # Pull all attachments (bug-level + comment-level), grouped per-comment.
    all_atts = list(db.scalars(
        select(Attachment).where(Attachment.bug_id == bug_id)
        .order_by(Attachment.created_at.asc())
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
                     item_type=getattr(bug, "item_type", None) or "Bug"),
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
    db.commit()

    fresh = db.scalar(_eager_bug(db).where(Bug.id == bug.id))
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
@router.put("/{bug_id}", response_model=BugOut)
def update_bug(
    bug_id: int,
    payload: BugUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> BugOut:
    bug = db.scalar(_eager_bug(db).where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail="Bug not found")

    if not can_edit_bug(actor, bug.reporter_id, [a.id for a in bug.assignees],
                        item_type=getattr(bug, "item_type", None) or "Bug"):
        # Tightened in v2.3: regular users can no longer edit Tasks or
        # Requirements. The error message uses the item's noun so the
        # toast makes sense to the user.
        raise HTTPException(
            status_code=403,
            detail=f"You don't have permission to edit this {(getattr(bug, 'item_type', None) or 'Bug').lower()}.",
        )

    fields = payload.model_dump(exclude_unset=True)
    actor_name = actor.name

    if "project_id" in fields and fields["project_id"] is not None:
        if db.get(Project, fields["project_id"]) is None:
            raise HTTPException(status_code=400, detail="Project does not exist")

    # event_id: validate when setting to a real ID; explicit null/0 unlinks.
    if "event_id" in fields and fields["event_id"]:
        if db.get(Event, fields["event_id"]) is None:
            raise HTTPException(status_code=400, detail="Event does not exist")
    if "event_id" in fields and fields["event_id"] == 0:
        # Treat 0 as a synonym for "unlink" for clients that can't easily
        # send a JSON null (FormData, querystring, etc.).
        fields["event_id"] = None

    assignee_ids = fields.pop("assignee_ids", None)
    has_reporter_in_payload = "reporter_id" in fields
    new_reporter_id = fields.pop("reporter_id", None)

    # ----- Reporter change permission gate (BUG-2 fix) -----
    # Only run the role check when the reporter would actually CHANGE.
    # Previously, the SPA always sent reporter_id in PUTs, which made
    # owner-edits 403 with "Only admins or managers can change the reporter"
    # even when they weren't trying to.
    reporter_actually_changes = (
        has_reporter_in_payload and new_reporter_id != bug.reporter_id
    )
    if reporter_actually_changes and actor.role not in ("admin", "manager"):
        raise HTTPException(
            status_code=403,
            detail="Only admins or managers can change the reporter",
        )

    # ----- Compute audit changes for tracked fields -----
    # Now includes `description` so a description-only edit no longer falls
    # through to the rollback branch (BUG-5 fix). All editable fields that
    # the API accepts are listed here.
    tracked = ["item_type", "status", "priority", "environment", "project_id",
               "due_date", "title", "description", "event_id"]
    changes: list[tuple[str, str, str]] = []
    for f in tracked:
        if f in fields and getattr(bug, f) != fields[f]:
            changes.append((f, str(getattr(bug, f) or ""), str(fields[f] or "")))

    # ----- Apply the simple field changes -----
    for key, value in fields.items():
        setattr(bug, key, value)

    # ----- Reporter change -----
    if reporter_actually_changes:
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

    # ----- Assignee diff -----
    newly_assigned: list[User] = []
    if assignee_ids is not None:
        new_users = _resolve_users(db, assignee_ids)
        old_ids = {a.id for a in bug.assignees}
        new_ids = {u.id for u in new_users}
        added_ids = new_ids - old_ids
        removed_ids = old_ids - new_ids
        if added_ids or removed_ids:
            old_names = sorted(a.name for a in bug.assignees)
            new_names = sorted(u.name for u in new_users)
            changes.append((
                "assignees",
                ", ".join(old_names) or "(none)",
                ", ".join(new_names) or "(none)",
            ))
            newly_assigned = [u for u in new_users if u.id in added_ids]
            bug.assignees = new_users   # only re-bind when actually different

    # ----- Commit / rollback -----
    if changes:
        # Prefix every change-log line with the bug id+title so searching
        # the audit trail by title hits update events too.
        prefix = f"#{bug.id} '{bug.title}' — "
        for field, old, new in changes:
            _log(
                db, bug.id, actor, f"{field}_changed",
                f"{prefix}{field}: '{old}' → '{new}'",
            )
        db.commit()
    else:
        # Nothing meaningful changed — discard any side-effecting setattrs
        # so we don't bump updated_at for a no-op PUT.
        db.rollback()

    fresh = db.scalar(_eager_bug(db).where(Bug.id == bug_id))
    snap = _bug_snapshot(fresh)

    if changes:
        background.add_task(
            notify_bug_updated, snap, list(changes), actor_name, actor.id,
        )
    if newly_assigned:
        background.add_task(
            notify_assignment, snap,
            tuple(UserSnapshot(id=u.id, name=u.name, email=u.email) for u in newly_assigned),
            actor_name,
        )

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
    bug = db.scalar(_eager_bug(db).where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail="Bug not found")
    # v3.1 spec: item deletion is admin-only across every type — managers
    # can edit, never delete. Reporters and assignees never could.
    if not can_delete_bug(actor, item_type=getattr(bug, "item_type", None) or "Bug"):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete items.",
        )
    title = bug.title
    itype = getattr(bug, "item_type", None) or "Bug"
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
        raise HTTPException(status_code=404, detail="Bug not found")
    comments = list(db.scalars(
        select(Comment).where(Comment.bug_id == bug_id)
        .order_by(Comment.created_at.asc(), Comment.id.asc())
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
    bug = db.scalar(_eager_bug(db).where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail="Bug not found")

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
        raise HTTPException(status_code=404, detail="Bug not found")
    if comment_id is not None:
        c = db.get(Comment, comment_id)
        if c is None or c.bug_id != bug_id:
            raise HTTPException(status_code=400, detail="Invalid comment_id for this bug")

    data = await _read_upload_with_limit(file, MAX_FILE_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    att = Attachment(
        bug_id=bug_id,
        comment_id=comment_id,
        uploader_user_id=uploader.id,
        uploader_name=uploader.name,
        filename=(file.filename or "unnamed")[:255],
        content_type=(file.content_type or "application/octet-stream")[:120],
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
    safe_ct = "application/octet-stream" if is_active else (a.content_type or "application/octet-stream")
    disposition = "attachment" if is_active else "inline"

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
    # Only admin/manager OR uploader OR person who can edit the bug.
    bug = db.scalar(_eager_bug(db).where(Bug.id == bug_id))
    can_delete = (
        actor.role in ("admin", "manager")
        or a.uploader_user_id == actor.id
        or (bug is not None and can_edit_bug(
            actor, bug.reporter_id, [u.id for u in bug.assignees],
            item_type=getattr(bug, "item_type", None) or "Bug",
        ))
    )
    if not can_delete:
        raise HTTPException(status_code=403, detail="You can't delete this attachment")
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
# Activity
# ---------------------------------------------------------------------------
@router.get("/{bug_id}/activity", response_model=list[ActivityOut])
def list_activity(
    bug_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[Activity]:
    if db.get(Bug, bug_id) is None:
        raise HTTPException(status_code=404, detail="Bug not found")
    return list(db.scalars(
        select(Activity).where(Activity.bug_id == bug_id)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
    ).all())
