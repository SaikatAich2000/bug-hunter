"""Stats / analytics API.

The dashboard KPI strip shows: Total | Open | Resolved | Closed | Resolve Later.

"Not a Bug" rows are excluded from the `bugs` total since they represent
reports that turned out not to be bugs. The rows are kept in the DB for audit /
history but hidden from the headline total.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Bug, Event, Project, User, bug_assignees
from app.reports.engine import (
    OPEN_STATUSES_BY_TYPE,
    RESOLVED_STATUSES_BY_TYPE,
    _utc_date,
)
from app.schemas import EXCLUDED_FROM_TOTAL_STATUSES, StatsOut

router = APIRouter(prefix="/api/stats", tags=["stats"])

_VALID_TYPES = {"Bug", "Requirement", "Task"}


def _by_project_stmt(item_type: Optional[str], status_filter: list[str]):
    """Bugs-per-project, scoped to item_type + status. Uses an OUTER join so
    projects with zero matching items still appear with count 0, which means
    every scope predicate goes on the join condition, not a WHERE clause."""
    join_cond = Bug.project_id == Project.id
    if item_type is not None:
        join_cond &= Bug.item_type == item_type
    if status_filter:
        join_cond &= Bug.status.in_(status_filter)
    return (
        select(Project.id, Project.name, Project.color, func.count(Bug.id))
        .outerjoin(Bug, join_cond)
        .group_by(Project.id, Project.name, Project.color)
        .order_by(func.count(Bug.id).desc())
    )


def _by_assignee_stmt(item_type: Optional[str], status_filter: list[str]):
    """Top-10 assignees by item count, scoped to item_type + status. The Bug
    table is joined only when a scope is active; the unscoped path counts
    assignment rows directly."""
    stmt = (
        select(User.id, User.name, User.email, func.count(bug_assignees.c.bug_id))
        .join(bug_assignees, bug_assignees.c.user_id == User.id)
    )
    if item_type is not None or status_filter:
        stmt = stmt.join(Bug, Bug.id == bug_assignees.c.bug_id)
        if item_type is not None:
            stmt = stmt.where(Bug.item_type == item_type)
        if status_filter:
            stmt = stmt.where(Bug.status.in_(status_filter))
    return (
        stmt.group_by(User.id, User.name, User.email)
        .order_by(func.count(bug_assignees.c.bug_id).desc())
        .limit(10)
    )


def _timeline_14d(db: Session, scoped_f):
    """Per-day created counts for the last 14 days (inclusive), as a dense list
    (zero-filled gaps). scoped_f applies the item_type + status scope."""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=13)
    # Sargable filter: compare the raw column to a datetime boundary so the
    # index on bugs.created_at can be used; wrapping the column in func.date()
    # would force a full scan, so func.date() stays only in SELECT/GROUP BY.
    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    # UTC-bucket the day: func.date of a timestamptz uses the session TZ on
    # Postgres, shifting the timeline by a day for a non-UTC session, so
    # _utc_date forces UTC to match the window keys built below.
    day_col = _utc_date(db, Bug.created_at)
    rows = db.execute(
        scoped_f(
            select(day_col, func.count(Bug.id))
            .where(Bug.created_at >= start_dt)
            .group_by(day_col)
        )
    ).all()
    counts_by_day: dict[str, int] = {}
    for raw_day, count in rows:
        key = raw_day if isinstance(raw_day, str) else raw_day.isoformat()
        counts_by_day[key] = int(count)
    return [
        {"date": (start + timedelta(days=i)).isoformat(),
         "count": counts_by_day.get((start + timedelta(days=i)).isoformat(), 0)}
        for i in range(14)
    ]


def _derive_kpis(
    by_status_global: dict[str, int], item_type: Optional[str],
) -> tuple[int, int, int, int]:
    """Headline KPI counts (open, resolved, closed, resolve_later) for the
    active scope. Per-type, so a Requirement's open/resolved use its own status
    set (New/In Review/Approved -> open, Implemented -> resolved) and a Task's
    its own (New/In Progress -> open, Done -> resolved). Closed and Resolve
    Later are Bug-only concepts, so they're 0 off the Bug tab."""
    if item_type and item_type != "Bug":
        open_count = sum(
            by_status_global.get(s, 0) for s in OPEN_STATUSES_BY_TYPE.get(item_type, [])
        )
        resolved_count = sum(
            by_status_global.get(s, 0) for s in RESOLVED_STATUSES_BY_TYPE.get(item_type, [])
        )
        return open_count, resolved_count, 0, 0
    open_count = sum(
        by_status_global.get(s, 0) for s in ("New", "In Progress", "Reopened")
    )
    return (
        open_count,
        by_status_global.get("Resolved", 0),
        by_status_global.get("Closed", 0),
        by_status_global.get("Resolve Later", 0),
    )


@router.get("", response_model=StatsOut)
def stats(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
    item_type: Optional[str] = Query(
        default=None,
        description=(
            "Scope every aggregation (KPIs, status/priority/env/project/"
            "assignee breakdowns and the 14-day timeline) to a single item "
            "type. Omit to get global stats. by_type and the event count "
            "stay global so the tab-count badges always match reality."
        ),
    ),
    status: Optional[list[str]] = Query(
        default=None,
        description=(
            "Scope the CHART breakdowns (by_status/priority/env/project/"
            "assignee + timeline) to one or more statuses — this is what makes "
            "the Analytics view react to a KPI tile click. The headline KPI "
            "counts (bugs/open/resolved/closed/resolve_later) stay GLOBAL so "
            "the strip still shows real totals and every tile stays toggleable."
        ),
    ),
) -> StatsOut:
    # The per-status KPIs are collapsed into a single GROUP BY status (one
    # round-trip instead of five), and those counts are reused for the
    # by_status breakdown to save another query.

    if item_type is not None and item_type not in _VALID_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"item_type must be one of {sorted(_VALID_TYPES)}",
        )

    # Normalize the status filter (drop blanks). Empty means no chart scoping.
    status_filter = [s for s in (status or []) if s]

    # Type-tab scoping: when item_type is set, every Bug-table aggregation
    # filters on Bug.item_type. by_type and the Event count stay global, since
    # the tab badges rely on the unfiltered totals.
    def _scoped(stmt):
        if item_type is not None:
            return stmt.where(Bug.item_type == item_type)
        return stmt

    # Chart scoping: item_type AND (optionally) the status filter. This is the
    # KPI-click filter that the Analytics charts react to.
    def _scoped_f(stmt):
        stmt = _scoped(stmt)
        if status_filter:
            stmt = stmt.where(Bug.status.in_(status_filter))
        return stmt

    # Global by_status (item_type scope only) drives the headline KPI counts,
    # which stay total and toggleable regardless of the chart status filter.
    by_status_global: dict[str, int] = {}
    for status_name, cnt in db.execute(
        _scoped(select(Bug.status, func.count(Bug.id)).group_by(Bug.status))
    ).all():
        by_status_global[status_name] = int(cnt)

    # Derive KPIs from the global set. Total excludes the statuses that don't
    # count as real bugs.
    bug_count = sum(
        c for s, c in by_status_global.items() if s not in EXCLUDED_FROM_TOTAL_STATUSES
    )
    open_count, resolved_count, closed_count, resolve_later_count = _derive_kpis(
        by_status_global, item_type,
    )

    # by_status chart: filtered when a status filter is active, else the same
    # result set already pulled for the KPIs (no extra query).
    if status_filter:
        by_status = {
            s: int(c)
            for s, c in db.execute(
                _scoped_f(select(Bug.status, func.count(Bug.id)).group_by(Bug.status))
            ).all()
        }
    else:
        by_status = by_status_global

    # Kept for backward compatibility: the dashboard no longer renders these,
    # but cached older clients may still read them.
    project_count = db.scalar(select(func.count(Project.id))) or 0
    user_count = db.scalar(select(func.count(User.id))) or 0

    # Cast keys/values like by_status above (consistency and NULL-key safety): a
    # bare dict() would leave raw DB ints and could surface a None key the
    # dict[str, int] response model rejects.
    by_priority = {
        str(p): int(c)
        for p, c in db.execute(
            _scoped_f(select(Bug.priority, func.count(Bug.id)).group_by(Bug.priority))
        ).all()
    }
    by_environment = {
        str(e): int(c)
        for e, c in db.execute(
            _scoped_f(select(Bug.environment, func.count(Bug.id)).group_by(Bug.environment))
        ).all()
    }
    # by_type stays global so the tab-count badges stay correct regardless of
    # the active tab. Coerce keys/values (str()/int()) so a NULL item_type can't
    # surface as a None key the dict[str, int] response model would reject.
    by_type = {
        str(t): int(c)
        for t, c in db.execute(
            select(Bug.item_type, func.count(Bug.id)).group_by(Bug.item_type)
        ).all()
    }
    events_count = db.scalar(select(func.count(Event.id))) or 0
    by_type["Event"] = int(events_count)

    by_project_rows = db.execute(
        _by_project_stmt(item_type, status_filter)
    ).all()
    by_assignee_rows = db.execute(
        _by_assignee_stmt(item_type, status_filter)
    ).all()

    timeline = _timeline_14d(db, _scoped_f)

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
        by_type=by_type,
        by_project=[{"id": pid, "name": name, "color": color, "count": int(cnt)}
                    for pid, name, color, cnt in by_project_rows],
        by_assignee=[{"id": uid, "name": name, "email": email, "count": int(cnt)}
                     for uid, name, email, cnt in by_assignee_rows],
        timeline=timeline,
    )
