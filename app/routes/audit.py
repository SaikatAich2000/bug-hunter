"""Global audit-trail endpoint — every action across the system."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Activity, User
from app.schemas import ActivityOut

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _like_escape(needle: str) -> str:
    """Escape SQL LIKE wildcards so a query containing literal '%' or '_'
    matches those characters exactly instead of acting as wildcards."""
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


@router.get("", response_model=list[ActivityOut])
def list_audit(
    entity_type: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    q: Optional[str] = None,
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[Activity]:
    stmt = select(Activity)
    if entity_type:
        stmt = stmt.where(Activity.entity_type == entity_type)
    if actor_user_id is not None:
        stmt = stmt.where(Activity.actor_user_id == actor_user_id)
    if q:
        like = f"%{_like_escape(q.lower())}%"
        stmt = stmt.where(or_(
            Activity.action.ilike(like, escape="\\"),
            Activity.detail.ilike(like, escape="\\"),
            Activity.actor_name.ilike(like, escape="\\"),
        ))
    stmt = stmt.order_by(Activity.created_at.desc(), Activity.id.desc()).limit(limit)
    return list(db.scalars(stmt).all())
