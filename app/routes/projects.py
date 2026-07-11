"""Projects API.

Permissions:
  - Read   : any authenticated user.
  - Create : admin or manager (require_manager_or_admin).
  - Update : admin or manager.
  - Delete : admin only.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import accessible_project_ids, add_user_project, can_access_project
from app.auth import get_current_user, require_admin, require_manager_or_admin
from app.database import get_db
from app.models import ROLE_ADMIN, Activity, Bug, Project, User
from app.schemas import ProjectIn, ProjectOut

router = APIRouter(prefix="/api/projects", tags=["projects"])


_DETAIL_PROJECT_NOT_FOUND = "Project not found"

def _audit(db: Session, actor: User, action: str, entity_id: int, detail: str) -> None:
    db.add(Activity(
        bug_id=None, entity_type="project", entity_id=entity_id,
        actor_user_id=actor.id, actor_name=actor.name,
        action=action, detail=detail,
    ))


@router.get("", response_model=list[ProjectOut])
def list_projects(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Project]:
    # None = admin (see all); restricted users see only their memberships
    accessible = accessible_project_ids(db, user)
    stmt = select(Project).order_by(func.lower(Project.name))
    if accessible is not None:
        stmt = stmt.where(Project.id.in_(accessible))
    return list(db.scalars(stmt.limit(500)).all())


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> Project:
    p = Project(**payload.model_dump())
    db.add(p)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Project name already exists") from exc
    # auto-enroll the creating manager so they keep visibility of their new project
    if actor.role != ROLE_ADMIN:
        add_user_project(db, actor.id, p.id)
    _audit(db, actor, "project_created", p.id, f"Created project '{p.name}'")
    db.commit()
    db.refresh(p)
    return p


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Project:
    p = db.get(Project, project_id)
    # 404 not 403 — scoping must not leak existence
    if p is None or not can_access_project(accessible_project_ids(db, user), project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_PROJECT_NOT_FOUND)
    return p


@router.put("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    payload: ProjectIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> Project:
    p = db.get(Project, project_id)
    # 404 not 403 — scoping must not leak existence
    if p is None or not can_access_project(accessible_project_ids(db, actor), project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_PROJECT_NOT_FOUND)
    # exclude_unset so omitted fields aren't reset to schema defaults
    fields = payload.model_dump(exclude_unset=True)
    changes = []
    for key, value in fields.items():
        old = getattr(p, key)
        if old != value:
            changes.append(f"{key}: {old!r} → {value!r}")
            setattr(p, key, value)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Project name already exists") from exc
    if changes:
        _audit(db, actor, "project_updated", p.id,
               f"Updated project '{p.name}': " + "; ".join(changes))
    db.commit()
    db.refresh(p)
    return p


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> dict[str, str]:
    # FOR UPDATE closes the count-then-delete race with a concurrent bug insert
    p = db.get(Project, project_id, with_for_update=True)
    if p is None:
        raise HTTPException(status_code=404, detail=_DETAIL_PROJECT_NOT_FOUND)

    bug_count = db.scalar(
        select(func.count(Bug.id)).where(Bug.project_id == project_id)
    ) or 0
    if bug_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: {bug_count} bug(s) belong to this project. Move or delete them first.",
        )
    name = p.name
    db.delete(p)
    _audit(db, actor, "project_deleted", project_id, f"Deleted project '{name}'")
    db.commit()
    return {"message": "Project deleted"}
