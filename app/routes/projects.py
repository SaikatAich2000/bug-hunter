"""Projects API."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Activity, Bug, Project, User
from app.schemas import ProjectIn, ProjectOut

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _audit(db: Session, actor: User | None, action: str, entity_id: int, detail: str) -> None:
    db.add(Activity(
        bug_id=None, entity_type="project", entity_id=entity_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action, detail=detail,
    ))


def _resolve_actor(db: Session, actor_user_id: Optional[int]) -> User | None:
    if actor_user_id is None: return None
    return db.get(User, actor_user_id)


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[Project]:
    return list(db.scalars(select(Project).order_by(func.lower(Project.name))).all())


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectIn,
    actor_user_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
) -> Project:
    p = Project(**payload.model_dump())
    db.add(p)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Project name already exists") from exc
    actor = _resolve_actor(db, actor_user_id)
    _audit(db, actor, "project_created", p.id, f"Created project '{p.name}'")
    db.commit()
    db.refresh(p)
    return p


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


@router.put("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    payload: ProjectIn,
    actor_user_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Project not found")
    fields = payload.model_dump()
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
        actor = _resolve_actor(db, actor_user_id)
        _audit(db, actor, "project_updated", p.id,
               f"Updated project '{p.name}': " + "; ".join(changes))
    db.commit()
    db.refresh(p)
    return p


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    actor_user_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Project not found")

    bug_count = db.scalar(
        select(func.count(Bug.id)).where(Bug.project_id == project_id)
    ) or 0
    if bug_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: {bug_count} bug(s) belong to this project. Move or delete them first.",
        )
    actor = _resolve_actor(db, actor_user_id)
    name = p.name
    db.delete(p)
    _audit(db, actor, "project_deleted", project_id, f"Deleted project '{name}'")
    db.commit()
    return {"message": "Project deleted"}
