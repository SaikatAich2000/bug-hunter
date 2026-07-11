"""Global audit-trail endpoint; managers and admins only."""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, cast, or_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.types import String

from app.access import accessible_project_ids
from app.auth import require_manager_or_admin
from app.database import get_db
from app.models import Activity, Bug, Event, User
from app.schemas import ActivityOut

router = APIRouter(prefix="/api/audit", tags=["audit"])

# int4 max; longer digit strings would overflow entity_id/bug_id on Postgres (500).
_MAX_PK_INT = 2**31 - 1


def _like_escape(needle: str) -> str:
    """Escape LIKE wildcards so literal '%' and '_' in user input match plainly."""
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


def _scope_to_projects(stmt, accessible):
    """Scope rows to a manager's projects; admins (accessible=None) see all.
    Aliased subqueries keep this composable with the search path's OUTER JOIN on bugs."""
    if accessible is None:
        return stmt
    sbug = aliased(Bug)
    sevent = aliased(Event)
    return stmt.where(or_(
        Activity.bug_id.in_(select(sbug.id).where(sbug.project_id.in_(accessible))),
        and_(
            Activity.entity_type == "event",
            Activity.entity_id.in_(
                select(sevent.id).where(sevent.project_id.in_(accessible))
            ),
        ),
    ))


@router.get("", response_model=list[ActivityOut])
def list_audit(
    entity_type: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=5000, le=10000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_manager_or_admin),
) -> list[Activity]:
    """Return audit events, optionally filtered by entity type, actor, or free-text `q`.
    The OUTER JOIN on bugs is added only when `q` is present to keep plain browsing cheap."""
    stmt = select(Activity)
    stmt = _scope_to_projects(stmt, accessible_project_ids(db, user))
    if entity_type:
        stmt = stmt.where(Activity.entity_type == entity_type)
    if actor_user_id is not None:
        stmt = stmt.where(Activity.actor_user_id == actor_user_id)
    if q:
        stmt = stmt.outerjoin(Bug, Bug.id == Activity.bug_id)
        raw = q.strip()
        like = f"%{_like_escape(raw.lower())}%"
        clauses = [
            Activity.action.ilike(like, escape="\\"),
            Activity.detail.ilike(like, escape="\\"),
            Activity.actor_name.ilike(like, escape="\\"),
            Activity.entity_type.ilike(like, escape="\\"),
            # Live bug title, so search still works after a rename.
            Bug.title.ilike(like, escape="\\"),
            Bug.item_type.ilike(like, escape="\\"),
        ]
        # Digit run so "#42", "bug 42", and "42" all resolve to id 42.
        digits_match = re.search(r"\d+", raw)
        if digits_match:
            digits = digits_match.group(0)
            # Skip exact-int compare above int4 max.
            if int(digits) <= _MAX_PK_INT:
                entity_id_val = int(digits)
                clauses.append(Activity.entity_id == entity_id_val)
                clauses.append(Activity.bug_id == entity_id_val)
            digit_like = f"%{_like_escape(digits)}%"
            clauses.append(cast(Activity.entity_id, String).ilike(digit_like, escape="\\"))
        stmt = stmt.where(or_(*clauses))
    stmt = (
        stmt.order_by(Activity.created_at.desc(), Activity.id.desc())
            .limit(limit)
            .offset(offset)
    )
    return list(db.scalars(stmt).all())
