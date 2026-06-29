"""Global audit-trail endpoint.

Managers and admins can read the trail; everyone else gets 403."""
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

# Upper bound for int4 PKs. A longer digit string in the search box would
# overflow entity_id / bug_id on Postgres and cause a 500, so exact-id
# comparison is skipped for values above this.
_MAX_PK_INT = 2**31 - 1


def _like_escape(needle: str) -> str:
    """Escape SQL LIKE special characters so literal '%' and '_' in user input
    are treated as plain characters, not wildcards."""
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


def _scope_to_projects(stmt, accessible):
    """Filter audit rows to a manager's assigned projects.

    Admins pass ``accessible=None`` and see everything. Managers see only rows
    tied to a bug or event in their project set; system/orphaned rows are hidden
    from them. Aliased subqueries keep this composable with the optional OUTER
    JOIN on bugs that the text-search path adds."""
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
    # The SPA defaults to 5000 and shows a "Load more" control for older rows.
    limit: int = Query(default=5000, le=10000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_manager_or_admin),
) -> list[Activity]:
    """Return audit events, optionally filtered by entity type, actor, or a free-text query.

    The search (`q`) matches broadly across actions, details, actor names, entity
    types, bug titles, and item types. Bug numbers are detected by extracting any
    digit run, so ``#42``, ``bug 42``, and plain ``42`` all hit the same row.

    The OUTER JOIN on bugs is deferred until `q` is present to keep plain
    paginated browsing cheap. The join is outer because most audit rows have no
    bug, and deleted bugs leave ``bug_id`` NULL while the original title persists
    in ``detail``."""
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
            # Current bug title — lets users search by the live title even
            # after a rename changed what the old detail strings say.
            Bug.title.ilike(like, escape="\\"),
            # Item type ("task", "requirement", "bug") so filtering by type word works.
            Bug.item_type.ilike(like, escape="\\"),
        ]
        # Pull the digit run so "#42", "bug 42", and "ticket #42" all resolve
        # to entity_id = 42. A substring LIKE on the cast column also handles
        # partial matches ("4" finds 4, 40, 41, ...).
        digits_match = re.search(r"\d+", raw)
        if digits_match:
            digits = digits_match.group(0)
            # Skip the exact-int compare for values that would overflow int4.
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
