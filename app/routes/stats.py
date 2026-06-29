"""Stats / analytics API.

Provides the KPI strip (Total | Open | Resolved | Closed | Resolve Later)
and all breakdown data for the Analytics view.

"Not a Bug" items are excluded from the bugs total but kept in the DB for
audit history.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.access import accessible_project_ids, scope_bug_query, scope_event_query
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


def _by_project_stmt(item_type: Optional[str], status_filter: list[str], accessible):
    """Return bugs-per-project, scoped to item_type and status.

    Uses an OUTER join so projects with zero matching items still appear with
    count 0. All scope predicates go on the join condition rather than a WHERE
    clause to preserve those zero rows. Restricted actors are limited to their
    own projects via a WHERE on Project.id.
    """
    join_cond = Bug.project_id == Project.id
    if item_type is not None:
        join_cond &= Bug.item_type == item_type
    if status_filter:
        join_cond &= Bug.status.in_(status_filter)
    stmt = (
        select(Project.id, Project.name, Project.color, func.count(Bug.id))
        .outerjoin(Bug, join_cond)
        .group_by(Project.id, Project.name, Project.color)
        .order_by(func.count(Bug.id).desc())
    )
    if accessible is not None:
        stmt = stmt.where(Project.id.in_(accessible))
    return stmt


def _by_assignee_stmt(item_type: Optional[str], status_filter: list[str], accessible):
    """Return the top 10 assignees by item count, scoped to item_type and status.

    The Bug table is only joined when a Bug-level scope is active (item_type,
    status, or project restriction); otherwise we count assignment rows directly.
    """
    stmt = (
        select(User.id, User.name, User.email, func.count(bug_assignees.c.bug_id))
        .join(bug_assignees, bug_assignees.c.user_id == User.id)
    )
    if item_type is not None or status_filter or accessible is not None:
        stmt = stmt.join(Bug, Bug.id == bug_assignees.c.bug_id)
        if item_type is not None:
            stmt = stmt.where(Bug.item_type == item_type)
        if status_filter:
            stmt = stmt.where(Bug.status.in_(status_filter))
        # Project scope — only reached when a restriction or another scope is
        # already active, so it's a no-op for unrestricted admins.
        stmt = scope_bug_query(stmt, accessible)
    return (
        stmt.group_by(User.id, User.name, User.email)
        .order_by(func.count(bug_assignees.c.bug_id).desc())
        .limit(10)
    )


def _timeline_14d(db: Session, scoped_f):
    """Return per-day created counts for the last 14 days as a dense, zero-filled list."""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=13)
    # Compare the raw column to a datetime boundary so the index on
    # bugs.created_at is usable. Wrapping it in func.date() would force a full
    # scan, so func.date() only appears in SELECT/GROUP BY via _utc_date.
    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    # func.date on a timestamptz uses the session timezone in Postgres, so a
    # non-UTC session shifts the buckets by a day. _utc_date forces UTC to keep
    # the keys consistent with the window built below.
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
    """Return headline KPI counts (open, resolved, closed, resolve_later).

    Each item type has its own open/resolved status set (e.g. Requirements use
    New/In Review/Approved as open, Implemented as resolved). Closed and
    Resolve Later are Bug-only, so they return 0 for other types.
    """
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
    user: User = Depends(get_current_user),
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
    # All per-status counts come from a single GROUP BY status query. Those
    # results are reused for the by_status breakdown to avoid a second query.

    if item_type is not None and item_type not in _VALID_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"item_type must be one of {sorted(_VALID_TYPES)}",
        )

    # Drop blanks; an empty list means no chart scoping.
    status_filter = [s for s in (status or []) if s]

    # Restricted managers/users only see their own projects; admins are unrestricted.
    accessible = accessible_project_ids(db, user)

    # _scoped applies item_type and project scope to any Bug-table statement,
    # covering KPIs and by_status.
    def _scoped(stmt):
        stmt = scope_bug_query(stmt, accessible)
        if item_type is not None:
            return stmt.where(Bug.item_type == item_type)
        return stmt

    # _scoped_f additionally applies the status filter (the KPI-tile click).
    # Used for chart breakdowns and the timeline.
    def _scoped_f(stmt):
        stmt = _scoped(stmt)
        if status_filter:
            stmt = stmt.where(Bug.status.in_(status_filter))
        return stmt

    # Pull by_status without the chart's status filter so the headline KPI
    # counts stay accurate and all tiles remain toggleable.
    by_status_global: dict[str, int] = {}
    for status_name, cnt in db.execute(
        _scoped(select(Bug.status, func.count(Bug.id)).group_by(Bug.status))
    ).all():
        by_status_global[status_name] = int(cnt)

    # Total excludes statuses that don't represent real bugs (e.g. "Not a Bug").
    bug_count = sum(
        c for s, c in by_status_global.items() if s not in EXCLUDED_FROM_TOTAL_STATUSES
    )
    open_count, resolved_count, closed_count, resolve_later_count = _derive_kpis(
        by_status_global, item_type,
    )

    # When a status filter is active, re-query for the chart. Otherwise reuse
    # the result already fetched for the KPIs.
    if status_filter:
        by_status = {
            s: int(c)
            for s, c in db.execute(
                _scoped_f(select(Bug.status, func.count(Bug.id)).group_by(Bug.status))
            ).all()
        }
    else:
        by_status = by_status_global

    # Kept for backward compatibility with older cached clients. project_count
    # reflects what the actor can see (membership count when restricted);
    # user_count is always global since it's a bare team-size figure.
    if accessible is not None:
        project_count = len(accessible)
    else:
        project_count = db.scalar(select(func.count(Project.id))) or 0
    user_count = db.scalar(select(func.count(User.id))) or 0

    # str()/int() casts guard against NULL keys (which the dict[str, int]
    # response model would reject) and ensure consistent Python types.
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
    # by_type covers all item types (drives the tab badges) but is project-scoped
    # so a restricted actor's counts only reflect their own projects.
    by_type = {
        str(t): int(c)
        for t, c in db.execute(
            scope_bug_query(
                select(Bug.item_type, func.count(Bug.id)).group_by(Bug.item_type),
                accessible,
            )
        ).all()
    }
    # Event badge is scoped to the actor's projects so it matches the Events view.
    events_count = db.scalar(
        scope_event_query(select(func.count(Event.id)), accessible)
    ) or 0
    by_type["Event"] = int(events_count)

    by_project_rows = db.execute(
        _by_project_stmt(item_type, status_filter, accessible)
    ).all()
    by_assignee_rows = db.execute(
        _by_assignee_stmt(item_type, status_filter, accessible)
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
