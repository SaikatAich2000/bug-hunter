"""Project-scoped access control.

Managers and regular users only see the work items, events, stats, reports and
audit entries belonging to the projects they're a member of (the
``user_projects`` table). Admins are NEVER restricted — they see everything,
regardless of membership.

The whole feature funnels through one primitive: ``accessible_project_ids``.
It returns:

  * ``None``        — the actor is unrestricted (an admin). No project filter is
                      applied anywhere, so admins keep the original flat view.
  * ``set[int]``    — the actor is restricted to exactly these project ids. The
                      set may be EMPTY, which means "sees nothing": a manager or
                      regular user with no memberships. (Existing accounts carry
                      no memberships after the additive upgrade, so they start
                      restricted-to-nothing until an admin tags them.)

Every list / detail / stats / report / audit path runs its Bug or Event query
through ``scope_bug_query`` / ``scope_event_query`` (or checks a single id with
``can_access_project``) so the boundary lives in exactly one place per entity
and can't drift between endpoints. The chat assistant's read handlers reuse the
same helpers, so Sleuth can't be a side door around the restriction.

This module is pure logic — it never raises HTTP errors. Routes decide the
status code (a resource outside your scope is surfaced as 404 / "does not
exist" so the restriction never leaks which projects or items exist).
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.models import ROLE_ADMIN, Bug, Event, User, user_projects


def accessible_project_ids(db: Session, user: User) -> Optional[set[int]]:
    """Project ids ``user`` may access, or ``None`` when unrestricted.

    Admins return ``None`` (no filter applied — they see everything). Managers
    and regular users return the set of project ids they're a member of; an
    untagged manager/user returns an empty set and therefore sees nothing.
    """
    if user.role == ROLE_ADMIN:
        return None
    rows = db.scalars(
        select(user_projects.c.project_id).where(user_projects.c.user_id == user.id)
    ).all()
    return {int(pid) for pid in rows}


def project_ids_for_user(db: Session, user_id: int) -> list[int]:
    """The project ids a user is tagged to, ascending. Used to echo a user's
    memberships back in the API and to render the edit form's current tags.
    Independent of role (an admin can still be explicitly tagged; the tags just
    don't restrict them)."""
    rows = db.scalars(
        select(user_projects.c.project_id)
        .where(user_projects.c.user_id == user_id)
        .order_by(user_projects.c.project_id)
    ).all()
    return [int(pid) for pid in rows]


def can_access_project(accessible: Optional[set[int]], project_id: Optional[int]) -> bool:
    """Whether a single project is in scope.

    Admins (``accessible is None``) always can. A restricted actor can only when
    ``project_id`` is among their memberships — and a ``None`` project (e.g. an
    event with no project assigned yet) is never in a restricted actor's scope.
    """
    if accessible is None:
        return True
    return project_id is not None and project_id in accessible


def scope_bug_query(stmt, accessible: Optional[set[int]]):
    """AND a project restriction onto a Bug ``select`` / ``count`` statement.

    No-op for admins (``accessible is None``). For a restricted actor the clause
    is ``Bug.project_id IN (...)``; an empty set produces an always-false
    ``IN ()`` so a tagless user matches no rows (sees nothing)."""
    if accessible is None:
        return stmt
    return stmt.where(Bug.project_id.in_(accessible))


def scope_event_query(stmt, accessible: Optional[set[int]]):
    """AND a project restriction onto an Event ``select`` / ``count`` statement.

    No-op for admins. Events with no project (``project_id`` NULL) never match a
    restricted actor's set, so they stay admin-only until a project is assigned.
    """
    if accessible is None:
        return stmt
    return stmt.where(Event.project_id.in_(accessible))


# ---------------------------------------------------------------------------
# Membership mutation — write rows into user_projects. The caller validates the
# project ids first (they exist + the editor may grant them) and owns the
# commit; these helpers only stage the row changes on the session.
# ---------------------------------------------------------------------------
def set_user_projects(db: Session, user_id: int, project_ids: Iterable[int]) -> None:
    """Replace ``user_id``'s memberships with exactly ``project_ids`` (deduped,
    order-preserving). Clears all when given an empty iterable. Does not commit."""
    db.execute(delete(user_projects).where(user_projects.c.user_id == user_id))
    for pid in dict.fromkeys(project_ids):
        db.execute(insert(user_projects).values(user_id=user_id, project_id=pid))


def add_user_project(db: Session, user_id: int, project_id: int) -> None:
    """Add a single membership if it isn't already present (idempotent). Used to
    auto-enroll a manager in a project they just created so they don't lose
    sight of it. Does not commit."""
    exists = db.scalar(
        select(user_projects.c.project_id).where(
            user_projects.c.user_id == user_id,
            user_projects.c.project_id == project_id,
        )
    )
    if exists is None:
        db.execute(insert(user_projects).values(user_id=user_id, project_id=project_id))


__all__ = [
    "accessible_project_ids",
    "project_ids_for_user",
    "can_access_project",
    "scope_bug_query",
    "scope_event_query",
    "set_user_projects",
    "add_user_project",
]
