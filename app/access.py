"""Project-scoped access control (pure logic, no HTTP).

``accessible_project_ids`` returns ``None`` for admins (unrestricted) or the set
of project ids a non-admin may see (empty set = sees nothing). Routes return
out-of-scope resources as 404 so the restriction doesn't leak what exists.
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.models import ROLE_ADMIN, Bug, Event, User, user_projects


def accessible_project_ids(db: Session, user: User) -> Optional[set[int]]:
    """Project ids ``user`` may access; ``None`` for admins, empty set = sees nothing."""
    if user.role == ROLE_ADMIN:
        return None
    rows = db.scalars(
        select(user_projects.c.project_id).where(user_projects.c.user_id == user.id)
    ).all()
    return {int(pid) for pid in rows}


def project_ids_for_user(db: Session, user_id: int) -> list[int]:
    """Project ids the user is tagged to, sorted ascending (role-agnostic)."""
    rows = db.scalars(
        select(user_projects.c.project_id)
        .where(user_projects.c.user_id == user_id)
        .order_by(user_projects.c.project_id)
    ).all()
    return [int(pid) for pid in rows]


def can_access_project(accessible: Optional[set[int]], project_id: Optional[int]) -> bool:
    """True if ``project_id`` is in scope; admins always pass, ``None`` project never matches restricted actors."""
    if accessible is None:
        return True
    return project_id is not None and project_id in accessible


def scope_bug_query(stmt, accessible: Optional[set[int]]):
    """Restrict a Bug statement to the actor's projects; no-op for admins."""
    if accessible is None:
        return stmt
    return stmt.where(Bug.project_id.in_(accessible))


def scope_event_query(stmt, accessible: Optional[set[int]]):
    """Restrict an Event statement to the actor's projects; NULL-project events stay admin-only."""
    if accessible is None:
        return stmt
    return stmt.where(Event.project_id.in_(accessible))


# Membership mutation helpers; callers validate project ids and commit.
def set_user_projects(db: Session, user_id: int, project_ids: Iterable[int]) -> None:
    """Replace the user's memberships with ``project_ids`` (deduped). Does not commit."""
    db.execute(delete(user_projects).where(user_projects.c.user_id == user_id))
    for pid in dict.fromkeys(project_ids):
        db.execute(insert(user_projects).values(user_id=user_id, project_id=pid))


def add_user_project(db: Session, user_id: int, project_id: int) -> None:
    """Add a membership if absent (idempotent). Does not commit."""
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
