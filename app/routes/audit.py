"""Global audit-trail endpoint — every action across the system.

Hidden from regular users per v3.1 spec. Managers and admins can read
the trail; everyone else gets 403."""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import cast, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.types import String

from app.auth import require_manager_or_admin
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
    _user: User = Depends(require_manager_or_admin),
) -> list[Activity]:
    """Returns audit events filtered by entity, actor and a free-text search.

    The search query (`q`) is broad on purpose so operators can paste in
    anything: bug numbers (`#42` / `42` / `bug 42`), user names, actions,
    entity types — they should all hit. We OR every plausible column
    together rather than parsing the query into a structured form."""
    stmt = select(Activity)
    if entity_type:
        stmt = stmt.where(Activity.entity_type == entity_type)
    if actor_user_id is not None:
        stmt = stmt.where(Activity.actor_user_id == actor_user_id)
    if q:
        raw = q.strip()
        like = f"%{_like_escape(raw.lower())}%"
        clauses = [
            Activity.action.ilike(like, escape="\\"),
            Activity.detail.ilike(like, escape="\\"),
            Activity.actor_name.ilike(like, escape="\\"),
            Activity.entity_type.ilike(like, escape="\\"),
        ]
        # Numeric IDs — strip "#", "bug", "issue", "ticket" prefixes so
        # "#42", "bug 42" and "ticket #42" all behave like a search for
        # entity_id = 42. We OR a `cast(entity_id) LIKE` clause too so
        # partial-id searches ("4" → 4, 40, 41, …, 422) still work.
        digits_match = re.search(r"\d+", raw)
        if digits_match:
            try:
                entity_id_val = int(digits_match.group(0))
                clauses.append(Activity.entity_id == entity_id_val)
            except ValueError:
                pass
            # Substring match on the entity_id column as text — lets the
            # user type "4" and find ids 4, 40, 41, … (handy when they
            # half-remember a bug number).
            digit_like = f"%{_like_escape(digits_match.group(0))}%"
            clauses.append(cast(Activity.entity_id, String).ilike(digit_like, escape="\\"))
        stmt = stmt.where(or_(*clauses))
    stmt = stmt.order_by(Activity.created_at.desc(), Activity.id.desc()).limit(limit)
    return list(db.scalars(stmt).all())
