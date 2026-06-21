"""Global audit-trail endpoint.

Managers and admins can read the trail; everyone else gets 403."""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import cast, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.types import String

from app.auth import require_manager_or_admin
from app.database import get_db
from app.models import Activity, Bug, User
from app.schemas import ActivityOut

router = APIRouter(prefix="/api/audit", tags=["audit"])

# Largest value a 32-bit signed PK column (entity_id / bug_id) can hold. A
# longer digit run in the search box would overflow int4 and raise a DataError
# (500) on Postgres, so we only do an exact-id compare at or below it.
_MAX_PK_INT = 2**31 - 1


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
    q: Optional[str] = Query(default=None, max_length=200),
    # Ceiling of 10000 bounds the response size; the SPA requests 5000 by
    # default and offers a "Load more" control for older activity.
    limit: int = Query(default=5000, le=10000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(require_manager_or_admin),
) -> list[Activity]:
    """Return audit events filtered by entity, actor and a free-text search.

    The search query (`q`) is matched broadly: bug numbers (`#42` / `42` /
    `bug 42`), user names, item titles, actions, entity types, and item types
    are all ORed together rather than parsed into a structured form.

    A text search needs the current bug title, supplied by an OUTER JOIN on
    bugs that is added only when `q` is present, so plain paginated browsing
    doesn't pay for it. The join is OUTER because most audit rows aren't
    bug-related and bug-related rows may have been detached (bug_id NULL) on
    bug delete; those rows still carry the original title in `detail`."""
    stmt = select(Activity)
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
            # Search the current bug title for rows still attached to a
            # live bug. Without this, renaming a bug after audit rows
            # were written would make the old detail strings the only
            # title users could search for.
            Bug.title.ilike(like, escape="\\"),
            # Item type ("task", "requirement", "bug") for the joined bug
            # so typing the type word filters down to that flavor of item.
            Bug.item_type.ilike(like, escape="\\"),
        ]
        # Numeric IDs: extract the digit run so "#42", "bug 42" and
        # "ticket #42" all search for entity_id = 42. A cast(entity_id) LIKE
        # clause is ORed in so partial-id searches ("4" -> 4, 40, 41, 422) work.
        digits_match = re.search(r"\d+", raw)
        if digits_match:
            digits = digits_match.group(0)
            # Exact-id compare only for values that fit int4 — a longer run
            # would overflow the entity_id / bug_id column and 500 on Postgres.
            # The substring LIKE below still searches a long/partial id as text.
            if int(digits) <= _MAX_PK_INT:
                entity_id_val = int(digits)
                clauses.append(Activity.entity_id == entity_id_val)
                # Also catch rows still attached to the bug via bug_id.
                clauses.append(Activity.bug_id == entity_id_val)
            # Substring match on the entity_id column as text, so typing "4"
            # finds ids 4, 40, 41, and so on.
            digit_like = f"%{_like_escape(digits)}%"
            clauses.append(cast(Activity.entity_id, String).ilike(digit_like, escape="\\"))
        stmt = stmt.where(or_(*clauses))
    stmt = (
        stmt.order_by(Activity.created_at.desc(), Activity.id.desc())
            .limit(limit)
            .offset(offset)
    )
    return list(db.scalars(stmt).all())
