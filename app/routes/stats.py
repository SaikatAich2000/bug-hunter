"""Stats / analytics API.

KPI strip on the dashboard now shows:
    Total | Open | Resolved | Closed | Resolve Later

Important: "Not a Bug" rows are explicitly EXCLUDED from the `bugs` total
because the product owner clarified that "Not a Bug" means the report
turned out not to be a bug at all, and therefore should not be counted.
The DB still keeps those rows around for audit / history; we just hide
them from the headline total.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Bug, Project, User, bug_assignees
from app.schemas import EXCLUDED_FROM_TOTAL_STATUSES, StatsOut

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("", response_model=StatsOut)
def stats(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> StatsOut:
    # Perf v3.2.1: this endpoint used to fire 11 separate count queries
    # (one per status KPI, one per dimension breakdown, plus projects /
    # users / timeline). We collapse the per-status KPIs into a single
    # GROUP BY status — exactly the same data, one round-trip instead
    # of five — and reuse those counts for the by_status breakdown so
    # we save another query there too. Net: 11 queries → 6.

    by_status: dict[str, int] = {}
    for status_name, cnt in db.execute(
        select(Bug.status, func.count(Bug.id)).group_by(Bug.status)
    ).all():
        by_status[status_name] = int(cnt)

    # Derive KPIs from the same single result set. Total excludes the
    # statuses the product owner said don't count as real bugs.
    bug_count = sum(
        c for s, c in by_status.items() if s not in EXCLUDED_FROM_TOTAL_STATUSES
    )
    open_count = sum(
        by_status.get(s, 0) for s in ("New", "In Progress", "Reopened")
    )
    resolved_count = by_status.get("Resolved", 0)
    closed_count = by_status.get("Closed", 0)
    resolve_later_count = by_status.get("Resolve Later", 0)

    # Kept for backward compatibility — the dashboard no longer renders these,
    # but in-flight clients (older cached JS) may still try to read them.
    project_count = db.scalar(select(func.count(Project.id))) or 0
    user_count = db.scalar(select(func.count(User.id))) or 0

    by_priority = dict(db.execute(
        select(Bug.priority, func.count(Bug.id)).group_by(Bug.priority)
    ).all())
    by_environment = dict(db.execute(
        select(Bug.environment, func.count(Bug.id)).group_by(Bug.environment)
    ).all())

    by_project_rows = db.execute(
        select(Project.id, Project.name, Project.color, func.count(Bug.id))
        .outerjoin(Bug, Bug.project_id == Project.id)
        .group_by(Project.id, Project.name, Project.color)
        .order_by(func.count(Bug.id).desc())
    ).all()

    by_assignee_rows = db.execute(
        select(User.id, User.name, User.email, func.count(bug_assignees.c.bug_id))
        .join(bug_assignees, bug_assignees.c.user_id == User.id)
        .group_by(User.id, User.name, User.email)
        .order_by(func.count(bug_assignees.c.bug_id).desc())
        .limit(10)
    ).all()

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=13)
    timeline_rows = db.execute(
        select(func.date(Bug.created_at), func.count(Bug.id))
        .where(func.date(Bug.created_at) >= start)
        .group_by(func.date(Bug.created_at))
    ).all()
    counts_by_day: dict[str, int] = {}
    for raw_day, count in timeline_rows:
        key = raw_day if isinstance(raw_day, str) else raw_day.isoformat()
        counts_by_day[key] = int(count)
    timeline = [
        {"date": (start + timedelta(days=i)).isoformat(),
         "count": counts_by_day.get((start + timedelta(days=i)).isoformat(), 0)}
        for i in range(14)
    ]

    return StatsOut(
        bugs=bug_count,
        open=open_count,
        resolved=resolved_count,
        closed=closed_count,
        resolve_later=resolve_later_count,
        projects=project_count,
        users=user_count,
        by_status=by_status,
        by_priority=by_priority,
        by_environment=by_environment,
        by_project=[{"id": pid, "name": name, "color": color, "count": int(cnt)}
                    for pid, name, color, cnt in by_project_rows],
        by_assignee=[{"id": uid, "name": name, "email": email, "count": int(cnt)}
                     for uid, name, email, cnt in by_assignee_rows],
        timeline=timeline,
    )
