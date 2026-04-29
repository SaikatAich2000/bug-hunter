"""Bugs API + comments + attachments + activity (per-bug)."""
from __future__ import annotations

import csv
import io
from typing import Optional

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException,
    Query, Response, UploadFile, status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
from app.email_service import (
    BugSnapshot, UserSnapshot,
    notify_assignment, notify_bug_created, notify_bug_updated, notify_comment_added,
)
from app.models import Activity, Attachment, Bug, Comment, Project, User
from app.schemas import (
    ActivityOut, AttachmentBrief, BugCreate, BugDetail, BugListResponse,
    BugOut, BugUpdate, CommentIn, CommentOut,
)

router = APIRouter(prefix="/api/bugs", tags=["bugs"])

# Soft cap on individual attachment size — protects the DB from a 4 GB video
# upload. Configurable via env if the team needs bigger files later.
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB


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


def _bug_to_out_dict(bug: Bug, attachment_count: int = 0) -> dict:
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
    }


def _bug_snapshot(bug: Bug) -> BugSnapshot:
    return BugSnapshot(
        id=bug.id, title=bug.title,
        project_name=bug.project.name if bug.project else "",
        status=bug.status, priority=bug.priority, severity=bug.environment,
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


# ---------------------------------------------------------------------------
# CSV export — must come before /{bug_id}
# ---------------------------------------------------------------------------
@router.get("/export.csv")
def export_bugs_csv(db: Session = Depends(get_db)) -> Response:
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
    project_id: Optional[int] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    priority: Optional[str] = None,
    environment: Optional[str] = None,
    reporter_id: Optional[int] = None,
    assignee_id: Optional[int] = None,
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
) -> BugListResponse:
    if page < 1 or page_size < 1 or page_size > 200:
        raise HTTPException(status_code=400, detail="Invalid pagination parameters")

    stmt = _eager_bug(db)
    count_stmt = select(func.count(Bug.id))

    def apply(both, clause):
        return both[0].where(clause), both[1].where(clause)

    if project_id is not None:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.project_id == project_id)
    if status_filter is not None:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.status == status_filter)
    if priority is not None:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.priority == priority)
    if environment is not None:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.environment == environment)
    if reporter_id is not None:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.reporter_id == reporter_id)
    if assignee_id is not None:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.assignees.any(User.id == assignee_id))
    if q:
        q_clean = q.strip().lstrip("#")
        if q_clean.isdigit():
            stmt, count_stmt = apply((stmt, count_stmt), Bug.id == int(q_clean))
        else:
            like = f"%{q.lower()}%"
            clause = or_(
                func.lower(Bug.title).like(like),
                func.lower(Bug.description).like(like),
            )
            stmt, count_stmt = apply((stmt, count_stmt), clause)

    total = db.scalar(count_stmt) or 0
    offset = (page - 1) * page_size
    stmt = stmt.order_by(Bug.updated_at.desc(), Bug.id.desc()).limit(page_size).offset(offset)
    bugs = list(db.scalars(stmt).all())

    items = []
    for b in bugs:
        items.append(_bug_to_out_dict(b, _attachment_count(db, b.id)))

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
def get_bug(bug_id: int, db: Session = Depends(get_db)) -> BugDetail:
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

    payload = _bug_to_out_dict(bug, len(all_atts))
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
) -> BugOut:
    if db.get(Project, payload.project_id) is None:
        raise HTTPException(status_code=400, detail="Project does not exist")

    reporter = _resolve_user(db, payload.reporter_id)
    assignees = _resolve_users(db, payload.assignee_ids)

    bug = Bug(
        project_id=payload.project_id,
        title=payload.title,
        description=payload.description,
        reporter_id=reporter.id if reporter else None,
        status=payload.status,
        priority=payload.priority,
        environment=payload.environment,
        due_date=payload.due_date,
    )
    bug.assignees = list(assignees)
    db.add(bug)
    db.flush()
    _log(db, bug.id, reporter, "bug_created", f"Bug created with status '{bug.status}'.")
    if assignees:
        names = ", ".join(a.name for a in assignees)
        _log(db, bug.id, reporter, "assignees_added", f"Assigned to: {names}")
    db.commit()

    fresh = db.scalar(_eager_bug(db).where(Bug.id == bug.id))
    snap = _bug_snapshot(fresh)
    actor_uid = reporter.id if reporter else None

    background.add_task(notify_bug_created, snap, actor_uid)
    if assignees:
        actor_name = reporter.name if reporter else "system"
        background.add_task(
            notify_assignment, snap,
            tuple(UserSnapshot(id=a.id, name=a.name, email=a.email) for a in assignees),
            actor_name,
        )

    return BugOut.model_validate(_bug_to_out_dict(fresh, 0))


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
@router.put("/{bug_id}", response_model=BugOut)
def update_bug(
    bug_id: int,
    payload: BugUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> BugOut:
    bug = db.scalar(_eager_bug(db).where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail="Bug not found")

    fields = payload.model_dump(exclude_unset=True)
    actor_user_id = fields.pop("actor_user_id", None)
    actor = _resolve_user(db, actor_user_id) if actor_user_id else None
    actor_name = actor.name if actor else "system"

    if "project_id" in fields and db.get(Project, fields["project_id"]) is None:
        raise HTTPException(status_code=400, detail="Project does not exist")

    assignee_ids = fields.pop("assignee_ids", None)
    new_reporter_id = fields.pop("reporter_id", None) if "reporter_id" in fields else "__omit__"

    tracked = ["status", "priority", "environment", "project_id", "due_date", "title"]
    changes: list[tuple[str, str, str]] = []
    for f in tracked:
        if f in fields and getattr(bug, f) != fields[f]:
            changes.append((f, str(getattr(bug, f) or ""), str(fields[f] or "")))

    for key, value in fields.items():
        setattr(bug, key, value)

    if new_reporter_id != "__omit__":
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
        bug.assignees = new_users

    if changes:
        for field, old, new in changes:
            _log(db, bug.id, actor, f"{field}_changed", f"{field}: '{old}' → '{new}'")
        db.commit()
    else:
        db.rollback()

    fresh = db.scalar(_eager_bug(db).where(Bug.id == bug_id))
    snap = _bug_snapshot(fresh)

    if changes:
        background.add_task(
            notify_bug_updated, snap, list(changes), actor_name,
            actor.id if actor else None,
        )
    if newly_assigned:
        background.add_task(
            notify_assignment, snap,
            tuple(UserSnapshot(id=u.id, name=u.name, email=u.email) for u in newly_assigned),
            actor_name,
        )

    return BugOut.model_validate(_bug_to_out_dict(fresh, _attachment_count(db, bug_id)))


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
@router.delete("/{bug_id}")
def delete_bug(
    bug_id: int,
    db: Session = Depends(get_db),
    actor_user_id: Optional[int] = Query(default=None),
) -> dict[str, str]:
    bug = db.get(Bug, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail="Bug not found")
    actor = _resolve_user(db, actor_user_id) if actor_user_id else None
    title = bug.title
    db.delete(bug)
    # Bug delete cascades comments/attachments/assignees, but the activity_log
    # is FK'd back to bug and would be deleted too. Log a non-bug audit row
    # so the global trail keeps a record.
    db.add(Activity(
        bug_id=None, entity_type="bug", entity_id=bug_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action="bug_deleted",
        detail=f"Deleted bug #{bug_id}: {title}",
    ))
    db.commit()
    return {"message": "Bug deleted"}


# ---------------------------------------------------------------------------
# Comments (with optional attachments)
# ---------------------------------------------------------------------------
@router.get("/{bug_id}/comments", response_model=list[CommentOut])
def list_comments(bug_id: int, db: Session = Depends(get_db)) -> list[dict]:
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
) -> dict:
    bug = db.scalar(_eager_bug(db).where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail="Bug not found")

    author = _resolve_user(db, payload.author_user_id)
    author_name = author.name if author else "anonymous"

    c = Comment(
        bug_id=bug_id,
        author_user_id=author.id if author else None,
        author_name=author_name,
        body=payload.body,
    )
    db.add(c)
    db.flush()
    _log(db, bug_id, author, "comment_added",
         f"Comment by {author_name}: {payload.body[:80]}")
    db.commit()
    db.refresh(c)

    snap = _bug_snapshot(bug)
    background.add_task(
        notify_comment_added, snap, author_name,
        author.id if author else None, payload.body,
    )
    return {
        "id": c.id, "bug_id": c.bug_id,
        "author_user_id": c.author_user_id, "author_name": c.author_name,
        "body": c.body, "created_at": c.created_at, "attachments": [],
    }


# ---------------------------------------------------------------------------
# Attachments — upload, list, download, delete
# ---------------------------------------------------------------------------
@router.post("/{bug_id}/attachments", response_model=AttachmentBrief, status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    bug_id: int,
    file: UploadFile = File(...),
    uploader_user_id: Optional[int] = Form(default=None),
    comment_id: Optional[int] = Form(default=None),
    db: Session = Depends(get_db),
) -> dict:
    bug = db.get(Bug, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail="Bug not found")
    if comment_id is not None:
        c = db.get(Comment, comment_id)
        if c is None or c.bug_id != bug_id:
            raise HTTPException(status_code=400, detail="Invalid comment_id for this bug")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max {MAX_FILE_BYTES // (1024 * 1024)} MB.",
        )

    uploader = _resolve_user(db, uploader_user_id) if uploader_user_id else None
    uploader_name = uploader.name if uploader else "anonymous"

    att = Attachment(
        bug_id=bug_id,
        comment_id=comment_id,
        uploader_user_id=uploader.id if uploader else None,
        uploader_name=uploader_name,
        filename=(file.filename or "unnamed")[:255],
        content_type=(file.content_type or "application/octet-stream")[:120],
        size_bytes=len(data),
        data=data,
    )
    db.add(att)
    db.flush()
    _log(
        db, bug_id, uploader, "attachment_added",
        f"{uploader_name} uploaded '{att.filename}' ({len(data)} bytes)"
        + (f" on comment #{comment_id}" if comment_id else ""),
        entity_type="attachment", entity_id=att.id,
    )
    db.commit()
    db.refresh(att)
    return _attachment_brief(att)


@router.get("/{bug_id}/attachments/{att_id}/download")
def download_attachment(bug_id: int, att_id: int, db: Session = Depends(get_db)):
    a = db.get(Attachment, att_id)
    if a is None or a.bug_id != bug_id:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # We use StreamingResponse rather than Response so big files don't load
    # entirely into the response buffer twice.
    return StreamingResponse(
        io.BytesIO(a.data),
        media_type=a.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{a.filename}"',
            "Content-Length": str(a.size_bytes),
        },
    )


@router.delete("/{bug_id}/attachments/{att_id}")
def delete_attachment(
    bug_id: int, att_id: int,
    db: Session = Depends(get_db),
    actor_user_id: Optional[int] = Query(default=None),
) -> dict:
    a = db.get(Attachment, att_id)
    if a is None or a.bug_id != bug_id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    actor = _resolve_user(db, actor_user_id) if actor_user_id else None
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
def list_activity(bug_id: int, db: Session = Depends(get_db)) -> list[Activity]:
    if db.get(Bug, bug_id) is None:
        raise HTTPException(status_code=404, detail="Bug not found")
    return list(db.scalars(
        select(Activity).where(Activity.bug_id == bug_id)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
    ).all())
