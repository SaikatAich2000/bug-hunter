"""Events API: containers for work items (standups, sprint meetings).

An event groups any number of items (Bug / Requirement / Task), linked via
`Bug.event_id` (nullable). The link is editable:

  - Create an item directly under an event:  POST /api/bugs with event_id
  - Move an existing item into an event:     PUT  /api/bugs/{id} {event_id: N}
  - Take an item back out:                   PUT  /api/bugs/{id} {event_id: null}

Deleting an event preserves its items via ``ondelete="SET NULL"`` on the model
column. The audit trail records every create/update/delete on the event itself
plus, separately, every item-side change of event_id (which routes/bugs.py
picks up as a normal field change).

Permissions:
  - create / edit: admin or manager
  - delete:        admin only
  - regular user:  read-only

Managers (event_managers M2M) are admin/manager-role users who own the event
and receive event-level notification emails (create / update / delete).
Per-task assignment emails fan out only to that task's assignees and do not cc
event managers, so adding someone as a manager doesn't subscribe them to every
task inside.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import notification_service
from app.access import (
    accessible_project_ids, can_access_project, scope_bug_query, scope_event_query,
)
from app.auth import can_delete_event, can_edit_bug, can_edit_event, get_current_user
from app.database import get_db
from app.email_service import (
    EventSnapshot, UserSnapshot,
    notify_event_created, notify_event_deleted, notify_event_updated,
)
from app.models import (
    ROLE_ADMIN, ROLE_MANAGER,
    Activity, Attachment, Bug, Event, Project, User, bug_assignees,
)
from app.schemas import (
    EventCreate, EventDetail, EventOut, EventUpdate,
)

router = APIRouter(prefix="/api/events", tags=["events"])


_DETAIL_EVENT_NOT_FOUND = "Event not found"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _log(
    db: Session, event_id: int | None, actor: User | None,
    action: str, detail: str,
) -> None:
    db.add(Activity(
        bug_id=None,
        entity_type="event",
        entity_id=event_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action,
        detail=detail,
    ))


def _user_brief(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role}


def _event_list_aggregates(
    db: Session, event_ids: list[int],
) -> tuple[dict[int, int], dict[int, int]]:
    """Grouped (item_count, assignee_count) maps for a page of events —
    two queries total instead of two per event in the list view."""
    if not event_ids:
        return {}, {}
    item_counts = {
        eid: int(n) for eid, n in db.execute(
            select(Bug.event_id, func.count(Bug.id))
            .where(Bug.event_id.in_(event_ids))
            .group_by(Bug.event_id)
        ).all()
    }
    assignee_counts = {
        eid: int(n) for eid, n in db.execute(
            select(Bug.event_id, func.count(func.distinct(bug_assignees.c.user_id)))
            .select_from(bug_assignees)
            .join(Bug, Bug.id == bug_assignees.c.bug_id)
            .where(Bug.event_id.in_(event_ids))
            .group_by(Bug.event_id)
        ).all()
    }
    return item_counts, assignee_counts


def _event_brief(
    db: Session, ev: Event,
    item_count: Optional[int] = None,
    assignee_count: Optional[int] = None,
    creator_names: Optional[dict[int, str]] = None,
) -> dict:
    if item_count is None:
        item_count = db.scalar(
            select(func.count(Bug.id)).where(Bug.event_id == ev.id)
        ) or 0
    if assignee_count is None:
        assignee_count = db.scalar(
            select(func.count(func.distinct(bug_assignees.c.user_id)))
            .select_from(bug_assignees)
            .join(Bug, Bug.id == bug_assignees.c.bug_id)
            .where(Bug.event_id == ev.id)
        ) or 0
    creator_name = None
    if ev.created_by_user_id:
        if creator_names is not None:
            creator_name = creator_names.get(ev.created_by_user_id)
        else:
            u = db.get(User, ev.created_by_user_id)
            creator_name = u.name if u else None
    return {
        "id": ev.id,
        "name": ev.name,
        "description": ev.description,
        "scheduled_for": ev.scheduled_for,
        "project_id": ev.project_id,
        "project_name": ev.project.name if ev.project else None,
        "created_by_user_id": ev.created_by_user_id,
        "created_by_name": creator_name,
        "item_count": int(item_count),
        "assignee_count": int(assignee_count),
        "managers": [_user_brief(m) for m in (ev.managers or [])],
        "created_at": ev.created_at,
        "updated_at": ev.updated_at,
    }


def _bug_to_brief(bug: Bug, actor: User, attachment_count: int = 0) -> dict:
    item_type = getattr(bug, "item_type", None) or "Bug"
    return {
        "id": bug.id,
        "project_id": bug.project_id,
        "project_name": bug.project.name if bug.project else None,
        "title": bug.title,
        "description": bug.description,
        "reporter": _user_brief(bug.reporter) if bug.reporter else None,
        "assignees": [_user_brief(a) for a in bug.assignees],
        "item_type": item_type,
        "status": bug.status,
        "priority": bug.priority,
        "environment": bug.environment,
        "due_date": bug.due_date,
        "event_id": bug.event_id,
        "event_name": bug.event.name if bug.event else None,
        "created_at": bug.created_at,
        "updated_at": bug.updated_at,
        "attachment_count": attachment_count,
        # Per-item permission, not a blanket True: a regular user can't edit a
        # Task/Requirement, so the SPA shouldn't render an edit affordance they
        # can't use. Matches the bugs endpoint's computed can_edit.
        "can_edit": can_edit_bug(
            actor, bug.reporter_id, [a.id for a in bug.assignees],
            item_type=item_type,
        ),
    }


def _resolve_managers(db: Session, ids: list[int]) -> list[User]:
    """Validate and return the User rows that match `ids`. Every id must point
    at an existing admin or manager user; regular users can't be event managers
    since they couldn't act on the event anyway.
    """
    if not ids:
        return []
    rows = db.scalars(select(User).where(User.id.in_(ids))).all()
    found = {u.id: u for u in rows}
    missing = set(ids) - set(found)
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown user ids: {sorted(missing)}")
    bad_roles = [u for u in rows if u.role not in (ROLE_ADMIN, ROLE_MANAGER)]
    if bad_roles:
        names = ", ".join(u.name for u in bad_roles)
        raise HTTPException(
            status_code=400,
            detail=f"Only admin or manager users can be event managers ({names} is not)",
        )
    return [found[i] for i in ids if i in found]


def _event_snapshot(ev: Event) -> EventSnapshot:
    return EventSnapshot(
        id=ev.id,
        name=ev.name,
        description=ev.description,
        scheduled_for=ev.scheduled_for,
        managers=tuple(
            UserSnapshot(id=m.id, name=m.name, email=m.email)
            for m in (ev.managers or [])
        ),
    )


def _require_edit(actor: User) -> None:
    if not can_edit_event(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins and managers can manage events.",
        )


def _validate_event_project(db: Session, accessible, project_id: int) -> None:
    """The event's owning project must exist and be in the actor's scope. An
    inaccessible (or missing) project is reported as "does not exist" so a
    restricted manager can't attach an event to a project they can't see."""
    if db.get(Project, project_id) is None or not can_access_project(accessible, project_id):
        raise HTTPException(status_code=400, detail="Project does not exist")


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@router.get("", response_model=list[EventOut])
def list_events(
    scheduled_for: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=500, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    stmt = select(Event).options(
        selectinload(Event.managers),
        selectinload(Event.project),
    ).order_by(
        # Events with no scheduled_for sort to the bottom; id desc is the stable
        # tiebreaker. nullslast() is available on SQLAlchemy 2.x and emulated on
        # SQLite.
        Event.scheduled_for.desc().nullslast(),
        Event.id.desc(),
    )
    # Project scope: a restricted manager/user only sees events for their
    # projects; an admin sees all. Project-less (legacy) events match no
    # restricted set, so they stay admin-only.
    stmt = scope_event_query(stmt, accessible_project_ids(db, user))
    if scheduled_for:
        stmt = stmt.where(Event.scheduled_for == scheduled_for)
    # Row ceiling with optional pagination so this list can't grow unbounded as
    # standups/sprints accrue. The default page_size (500) covers any realistic
    # board.
    stmt = stmt.limit(page_size).offset((page - 1) * page_size)
    rows = list(db.scalars(stmt).all())
    item_counts, assignee_counts = _event_list_aggregates(
        db, [ev.id for ev in rows])
    creator_ids = {ev.created_by_user_id for ev in rows if ev.created_by_user_id}
    creator_names: dict[int, str] = {}
    if creator_ids:
        creator_names = dict(db.execute(
            select(User.id, User.name).where(User.id.in_(creator_ids))
        ).all())
    return [
        _event_brief(
            db, ev,
            item_count=item_counts.get(ev.id, 0),
            assignee_count=assignee_counts.get(ev.id, 0),
            creator_names=creator_names,
        )
        for ev in rows
    ]


# ---------------------------------------------------------------------------
# Detail (event + its items)
# ---------------------------------------------------------------------------
# Ceiling on items serialized for one event-detail response. Any user can add
# an item to an event, so without a cap a large event would stream its entire
# eager-loaded item set into one response. The exact count is still shown via a
# separate COUNT, so truncation is visible.
_EVENT_ITEMS_MAX = 1000


@router.get("/{event_id}", response_model=EventDetail)
def get_event(
    event_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    accessible = accessible_project_ids(db, user)
    ev = db.scalar(
        select(Event).options(selectinload(Event.managers), selectinload(Event.project))
        .where(Event.id == event_id)
    )
    # An event outside the actor's scope reads as not-found.
    if ev is None or not can_access_project(accessible, ev.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_EVENT_NOT_FOUND)
    items_stmt = select(Bug).options(
        selectinload(Bug.project),
        selectinload(Bug.reporter),
        selectinload(Bug.assignees),
        selectinload(Bug.event),
    # Newest item first, so the most recently updated item is at the top.
    ).where(Bug.event_id == event_id).order_by(
        Bug.updated_at.desc(), Bug.id.desc(),
    ).limit(_EVENT_ITEMS_MAX)
    # Defense in depth: an event may hold items from other projects, so still
    # filter the items themselves to the actor's scope (no-op for admins).
    items_stmt = scope_bug_query(items_stmt, accessible)
    items = list(db.scalars(items_stmt).all())
    total_items = db.scalar(
        scope_bug_query(
            select(func.count(Bug.id)).where(Bug.event_id == event_id), accessible,
        )
    ) or 0
    # One aggregate query for attachment counts across every item — avoids
    # an N+1 round-trip when the event has many tasks.
    bug_ids = [b.id for b in items]
    if bug_ids:
        att_counts = dict(db.execute(
            select(Attachment.bug_id, func.count(Attachment.id))
            .where(Attachment.bug_id.in_(bug_ids))
            .group_by(Attachment.bug_id)
        ).all())
    else:
        att_counts = {}
    # Pass the count so _event_brief doesn't re-issue a COUNT(*) for the detail
    # view.
    payload = _event_brief(db, ev, item_count=total_items)
    payload["items"] = [_bug_to_brief(b, user, int(att_counts.get(b.id, 0))) for b in items]
    # Surface when the item list was capped at _EVENT_ITEMS_MAX so the client
    # can show "showing N of M" rather than a silently lossy view.
    payload["items_truncated"] = total_items > len(items)
    return payload


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
def create_event(
    payload: EventCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    _require_edit(actor)
    # When a project is supplied it must exist and be in the creator's scope. A
    # project-less event is allowed (it ends up admin-only); the SPA always
    # supplies one.
    if payload.project_id is not None:
        _validate_event_project(db, accessible_project_ids(db, actor), payload.project_id)
    managers = _resolve_managers(db, payload.manager_ids or [])
    ev = Event(
        name=payload.name,
        description=payload.description,
        scheduled_for=payload.scheduled_for,
        project_id=payload.project_id,
        created_by_user_id=actor.id,
    )
    if managers:
        ev.managers = managers
    db.add(ev)
    db.flush()
    _log(db, ev.id, actor, "event_created",
         f"Event created: {ev.name}"
         + (f" (scheduled for {ev.scheduled_for})" if ev.scheduled_for else ""))
    # In-app notifications to the managers. No exclude=actor: being made a
    # manager is meaningful even for a creator who added themselves, so the
    # creator is notified if they're in the manager list.
    if managers:
        notification_service.notify(
            db, [m.id for m in managers], kind="event", background=background,
            title=f"Added as manager on “{ev.name}”",
            body=f"You're a manager on the event “{ev.name}” created by {actor.name}.",
            event_id=ev.id, actor_name=actor.name,
        )
    db.commit()
    db.refresh(ev)

    if managers:
        snap = _event_snapshot(ev)
        background.add_task(notify_event_created, snap, actor.name, actor.id)
    return _event_brief(db, ev)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
_EVENT_TRACKED_FIELDS = ["name", "description", "scheduled_for"]


def _compute_event_changes(ev: Event, fields: dict) -> list[tuple[str, str, str]]:
    changes: list[tuple[str, str, str]] = []
    for f in _EVENT_TRACKED_FIELDS:
        if f in fields and getattr(ev, f) != fields[f]:
            changes.append((f, str(getattr(ev, f) or ""), str(fields[f] or "")))
    return changes


def _apply_event_manager_diff(ev: Event, db: Session, new_manager_ids: Optional[list[int]],
                              changes: list[tuple[str, str, str]]) -> None:
    """Set-compare ignores order so re-sending the same list isn't a change."""
    if new_manager_ids is None:
        return
    old_ids = sorted({m.id for m in (ev.managers or [])})
    new_ids = sorted(set(new_manager_ids))
    if old_ids == new_ids:
        return
    new_managers = _resolve_managers(db, new_manager_ids)
    old_names = sorted(m.name for m in (ev.managers or []))
    new_names = sorted(m.name for m in new_managers)
    changes.append((
        "managers",
        ", ".join(old_names) or "(none)",
        ", ".join(new_names) or "(none)",
    ))
    ev.managers = new_managers


def _persist_event_update(db: Session, ev: Event, actor: User,
                          changes: list[tuple[str, str, str]]) -> None:
    if not changes:
        db.rollback()
        return
    for field, old, new in changes:
        _log(db, ev.id, actor, f"event_{field}_changed",
             f"{field}: '{old}' → '{new}'")
    db.commit()


@router.put("/{event_id}", response_model=EventOut)
def update_event(
    event_id: int,
    payload: EventUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    _require_edit(actor)
    accessible = accessible_project_ids(db, actor)
    ev = db.scalar(
        select(Event).options(selectinload(Event.managers), selectinload(Event.project))
        .where(Event.id == event_id)
    )
    # Can't edit (or probe the existence of) an event outside the actor's scope.
    if ev is None or not can_access_project(accessible, ev.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_EVENT_NOT_FOUND)

    fields = payload.model_dump(exclude_unset=True)
    # project_id is handled specially: validate the new project is in scope, then
    # apply it as a tracked change. Popped so the generic setattr loop skips it.
    new_project_id = fields.pop("project_id", None)
    if new_project_id is not None:
        _validate_event_project(db, accessible, new_project_id)
    changes = _compute_event_changes(ev, fields)
    new_manager_ids = fields.pop("manager_ids", None)
    # Capture the manager set before the diff so a just-removed manager, whose
    # removal is the change being logged, is still notified rather than dropped
    # from the recipient list.
    old_manager_ids = [m.id for m in ev.managers]
    for k, v in fields.items():
        setattr(ev, k, v)
    if new_project_id is not None and ev.project_id != new_project_id:
        changes.append(("project", str(ev.project_id or ""), str(new_project_id)))
        ev.project_id = new_project_id
    _apply_event_manager_diff(ev, db, new_manager_ids, changes)
    recipient_ids = sorted({*old_manager_ids, *(m.id for m in ev.managers)})
    if changes and recipient_ids:
        _summary = ", ".join(f for f, _, _ in changes)
        notification_service.notify(
            db, recipient_ids, kind="event", background=background,
            title=f"Event “{ev.name}” updated",
            body=f"{actor.name} changed {_summary}.",
            event_id=ev.id, actor_name=actor.name, exclude=actor.id,
        )
    _persist_event_update(db, ev, actor, changes)

    ev = db.scalar(
        select(Event).options(selectinload(Event.managers), selectinload(Event.project))
        .where(Event.id == event_id)
    )
    if changes and ev.managers:
        snap = _event_snapshot(ev)
        background.add_task(
            notify_event_updated, snap, list(changes), actor.name, actor.id,
        )
    return _event_brief(db, ev)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
@router.delete("/{event_id}")
def delete_event(
    event_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict[str, str]:
    ev = db.scalar(
        select(Event).options(selectinload(Event.managers)).where(Event.id == event_id)
    )
    if ev is None:
        raise HTTPException(status_code=404, detail=_DETAIL_EVENT_NOT_FOUND)
    if not can_delete_event(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete events.",
        )
    name = ev.name
    manager_ids = [m.id for m in (ev.managers or [])]
    snap = _event_snapshot(ev) if ev.managers else None
    # Items keep existing. The FK is declared ondelete='SET NULL', but they are
    # also explicitly nulled here because SQLite doesn't always honour the FK
    # ondelete for attached relationships on a populated session.
    db.query(Bug).filter(Bug.event_id == event_id).update(
        {Bug.event_id: None}, synchronize_session=False,
    )
    db.delete(ev)
    # Pass event_id (not None) so the deletion is findable by the audit screen's
    # entity-id lookup ("event N"); entity_id is plain metadata, so it can point
    # at the now-deleted event.
    _log(db, event_id, actor, "event_deleted",
         f"Deleted event #{event_id}: {name}")
    # No event_id on these rows: the event is gone and the FK cascade would
    # immediately delete any notification that referenced it.
    if manager_ids:
        notification_service.notify(
            db, manager_ids, kind="event", background=background,
            title=f"Event “{name}” deleted",
            body=f"{actor.name} deleted this event.",
            actor_name=actor.name, exclude=actor.id,
        )
    db.commit()

    if snap is not None:
        background.add_task(notify_event_deleted, snap, actor.name, actor.id)
    return {"message": "Event deleted"}
