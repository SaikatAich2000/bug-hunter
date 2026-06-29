"""Project-scoped access control.

Managers and regular users are limited to work items, events, stats, reports,
and audit entries that belong to projects they're a member of (via the
``user_projects`` table). Admins are never restricted.

The central primitive is ``accessible_project_ids``, which returns:

  * ``None``       — the actor is an admin; no project filter is applied.
  * ``set[int]``   — the actor is restricted to these project ids. An empty
                     set means "sees nothing" (a manager or user with no
                     memberships). Accounts that predate the additive migration
                     carry no memberships and start restricted-to-nothing until
                     an admin tags them.

Every list, detail, stats, report, and audit path filters Bug and Event queries
through ``scope_bug_query`` / ``scope_event_query`` (or checks a single id with
``can_access_project``), so the enforcement lives in one place and can't drift
between endpoints. The Sleuth chat assistant reuses the same helpers, so it
can't act as a side door around the restriction.

This module is pure logic and never raises HTTP errors. Routes are responsible
for the status code — out-of-scope resources are returned as 404 so the
restriction doesn't leak which projects or items exist.
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.models import ROLE_ADMIN, Bug, Event, User, user_projects


def accessible_project_ids(db: Session, user: User) -> Optional[set[int]]:
    """Return the project ids ``user`` may access, or ``None`` for admins (unrestricted).

    Non-admin users get the set of projects they're tagged to. An empty set
    means no memberships, so the user sees nothing.
    """
    if user.role == ROLE_ADMIN:
        return None
    rows = db.scalars(
        select(user_projects.c.project_id).where(user_projects.c.user_id == user.id)
    ).all()
    return {int(pid) for pid in rows}


def project_ids_for_user(db: Session, user_id: int) -> list[int]:
    """Return the project ids a user is tagged to, sorted ascending.

    Used to echo memberships in the API and populate the edit form. Role is
    irrelevant here — an admin can be explicitly tagged, but those tags don't
    restrict them.
    """
    rows = db.scalars(
        select(user_projects.c.project_id)
        .where(user_projects.c.user_id == user_id)
        .order_by(user_projects.c.project_id)
    ).all()
    return [int(pid) for pid in rows]


def can_access_project(accessible: Optional[set[int]], project_id: Optional[int]) -> bool:
    """Return True if ``project_id`` is within the actor's scope.

    Admins (``accessible is None``) always pass. For restricted actors, a
    ``None`` project_id (e.g. an event not yet assigned to a project) never
    matches.
    """
    if accessible is None:
        return True
    return project_id is not None and project_id in accessible


def scope_bug_query(stmt, accessible: Optional[set[int]]):
    """Apply a project restriction to a Bug ``select`` / ``count`` statement.

    No-ops for admins. For restricted actors, appends ``Bug.project_id IN (...)``.
    An empty set produces an always-false ``IN ()`` so a tagless user matches nothing.
    """
    if accessible is None:
        return stmt
    return stmt.where(Bug.project_id.in_(accessible))


def scope_event_query(stmt, accessible: Optional[set[int]]):
    """Apply a project restriction to an Event ``select`` / ``count`` statement.

    No-ops for admins. Events with a NULL ``project_id`` never match a
    restricted actor's membership set, so they remain admin-only until assigned.
    """
    if accessible is None:
        return stmt
    return stmt.where(Event.project_id.in_(accessible))


# Membership mutation helpers. The caller is responsible for validating project
# ids and committing the session; these functions only stage the row changes.
def set_user_projects(db: Session, user_id: int, project_ids: Iterable[int]) -> None:
    """Replace ``user_id``'s memberships with exactly ``project_ids`` (deduped, order-preserving).

    Passing an empty iterable clears all memberships. Does not commit.
    """
    db.execute(delete(user_projects).where(user_projects.c.user_id == user_id))
    for pid in dict.fromkeys(project_ids):
        db.execute(insert(user_projects).values(user_id=user_id, project_id=pid))


def add_user_project(db: Session, user_id: int, project_id: int) -> None:
    """Add a single membership if it isn't already present (idempotent).

    Used to auto-enroll a manager in a project they just created so they don't
    immediately lose sight of it. Does not commit.
    """
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
