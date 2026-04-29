"""Stats / analytics API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Bug, Project, User, bug_assignees
from app.schemas import StatsOut

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("", response_model=StatsOut)
def stats(db: Session = Depends(get_db)) -> StatsOut:
    project_count = db.scalar(select(func.count(Project.id))) or 0
    bug_count = db.scalar(select(func.count(Bug.id))) or 0
    user_count = db.scalar(select(func.count(User.id))) or 0

    open_count = db.scalar(
        select(func.count(Bug.id)).where(Bug.status.in_(("New", "In Progress", "Reopened")))
    ) or 0
    resolved_count = db.scalar(
        select(func.count(Bug.id)).where(Bug.status.in_(("Resolved", "Closed")))
    ) or 0

    by_status = dict(db.execute(
        select(Bug.status, func.count(Bug.id)).group_by(Bug.status)
    ).all())
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
        projects=project_count, bugs=bug_count, users=user_count,
        open=open_count, resolved=resolved_count,
        by_status=by_status, by_priority=by_priority, by_environment=by_environment,
        by_project=[{"id": pid, "name": name, "color": color, "count": int(cnt)}
                    for pid, name, color, cnt in by_project_rows],
        by_assignee=[{"id": uid, "name": name, "email": email, "count": int(cnt)}
                     for uid, name, email, cnt in by_assignee_rows],
        timeline=timeline,
    )
