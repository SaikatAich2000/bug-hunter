"""Bugs API + comments + attachments + activity (per-bug)."""
from __future__ import annotations

import csv
import io
import re
from typing import Optional
from urllib.parse import quote

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException,
    Query, Response, UploadFile, status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import can_edit_bug, get_current_user
from app.database import get_db
from app.email_service import (
    BugSnapshot, UserSnapshot,
    notify_assignment, notify_bug_created, notify_bug_updated, notify_comment_added,
)
from app.models import Activity, Attachment, Bug, Comment, Project, User
from app.schemas import (
    ALLOWED_ENVIRONMENTS, ALLOWED_PRIORITIES, ALLOWED_STATUSES,
    ActivityOut, AttachmentBrief, BugCreate, BugDetail, BugListResponse,
    BugOut, BugUpdate, CommentIn, CommentOut, normalize_choice,
)

router = APIRouter(prefix="/api/bugs", tags=["bugs"])

# Soft cap on individual attachment size — protects the DB from a 4 GB video
# upload. Configurable via env if the team needs bigger files later.
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

# Read uploads in 1 MB chunks so we abort over-sized requests before
# they consume RAM. Anything above MAX_FILE_BYTES is rejected mid-stream.
_UPLOAD_CHUNK = 1024 * 1024

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
_HEADER_FILENAME_BAD = re.compile(r'[\r\n"\\]+')


def _safe_filename_for_header(name: str) -> str:
    """Strip CR/LF/quotes/backslashes from a filename so it can't break
    the Content-Disposition header. Returns ASCII-safe form with the
    original (possibly-Unicode) form preserved via filename* per RFC 5987."""
    return _HEADER_FILENAME_BAD.sub("_", name) or "file"


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
        "status": bug.status,
        "priority": bug.priority,
        "environment": bug.environment,
        "due_date": bug.due_date,
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
        "id", "project", "title", "status", "priority", "environment",
        "reporter_name", "reporter_email", "assignees", "due_date",
        "created_at", "updated_at", "description",
    ])
    for b in rows:
        writer.writerow([
            b.id,
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
    reporter_id: Optional[int] = None,
    assignee_id: Optional[list[int]] = Query(default=None),
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
    if reporter_id is not None:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.reporter_id == reporter_id)
    if assignee_ids:
        stmt, count_stmt = apply(
            (stmt, count_stmt),
            Bug.assignees.any(User.id.in_(assignee_ids)),
        )
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
            can_edit_bug(_user, b.reporter_id, [a.id for a in b.assignees]),
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
        can_edit_bug(user, bug.reporter_id, [a.id for a in bug.assignees]),
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
            raise HTTPException(status_code=403, detail="You can only file bugs as yourself")
        reporter = _resolve_user(db, payload.reporter_id)
    else:
        reporter = actor

    assignees = _resolve_users(db, payload.assignee_ids)

    bug = Bug(
        project_id=payload.project_id,
        title=payload.title,
        description=payload.description,
        reporter_id=reporter.id,
        status=payload.status,
        priority=payload.priority,
        environment=payload.environment,
        due_date=payload.due_date,
    )
    bug.assignees = list(assignees)
    db.add(bug)
    db.flush()
    _log(db, bug.id, actor, "bug_created", f"Bug created with status '{bug.status}'.")
    if assignees:
        names = ", ".join(a.name for a in assignees)
        _log(db, bug.id, actor, "assignees_added", f"Assigned to: {names}")
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
        can_edit_bug(actor, fresh.reporter_id, [a.id for a in fresh.assignees]),
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

    if not can_edit_bug(actor, bug.reporter_id, [a.id for a in bug.assignees]):
        raise HTTPException(
            status_code=403,
            detail="You can only edit bugs you reported or are assigned to",
        )

    fields = payload.model_dump(exclude_unset=True)
    actor_name = actor.name

    if "project_id" in fields and fields["project_id"] is not None:
        if db.get(Project, fields["project_id"]) is None:
            raise HTTPException(status_code=400, detail="Project does not exist")

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
    tracked = ["status", "priority", "environment", "project_id",
               "due_date", "title", "description"]
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
        for field, old, new in changes:
            _log(db, bug.id, actor, f"{field}_changed", f"{field}: '{old}' → '{new}'")
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
        can_edit_bug(actor, fresh.reporter_id, [a.id for a in fresh.assignees]),
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
    if actor.role not in ("admin", "manager"):
        raise HTTPException(
            status_code=403,
            detail="Only admins and managers can delete bugs.",
        )
    title = bug.title
    db.delete(bug)
    # Bug delete cascades comments/attachments/assignees, but the activity_log
    # is FK'd back to bug and would be deleted too. Log a non-bug audit row
    # so the global trail keeps a record.
    db.add(Activity(
        bug_id=None, entity_type="bug", entity_id=bug_id,
        actor_user_id=actor.id, actor_name=actor.name,
        action="bug_deleted",
        detail=f"Deleted bug #{bug_id}: {title}",
    ))
    db.commit()
    return {"message": "Bug deleted"}


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
         f"Comment by {author.name}: {payload.body[:80]}")
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
        or (bug is not None and can_edit_bug(actor, bug.reporter_id, [u.id for u in bug.assignees]))
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
