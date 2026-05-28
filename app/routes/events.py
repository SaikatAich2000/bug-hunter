"""Events API — containers for work items (standups, sprint meetings).

An event groups any number of items (Bug / Requirement / Task) so the
morning standup can be tracked as a first-class entity. Items are linked
via `Bug.event_id` (nullable). The link is fully editable:

  - Create an item directly under an event:  POST /api/bugs with event_id
  - Move an existing item into an event:     PUT  /api/bugs/{id} {event_id: N}
  - Take an item back out:                   PUT  /api/bugs/{id} {event_id: null}

Deleting an event preserves its items — the FK is set to NULL via the
SQL ``ondelete="SET NULL"`` on the model column. The audit trail records
every create/update/delete on the event itself plus, separately, every
item-side change of event_id (which routes/bugs.py picks up as a normal
field change).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth import get_current_user
from app.database import get_db
from app.models import Activity, Bug, Event, User, bug_assignees
from app.schemas import (
    BugOut, EventCreate, EventDetail, EventOut, EventUpdate,
)

router = APIRouter(prefix="/api/events", tags=["events"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _log(
    db: Session, event_id: int | None, actor: User | None,
    action: str, detail: str,
) -> None:
    """Append an audit row for an event-level action.

    Audit rows for events use entity_type='event'; routes/bugs.py keeps
    entity_type='bug' for its own actions. Together they show up in the
    global audit feed under their respective filters.
    """
    db.add(Activity(
        bug_id=None,
        entity_type="event",
        entity_id=event_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action,
        detail=detail,
    ))


def _event_brief(db: Session, ev: Event) -> dict:
    """Brief used for list-endpoint rows."""
    item_count = db.scalar(
        select(func.count(Bug.id)).where(Bug.event_id == ev.id)
    ) or 0
    # Count distinct assignees across all items in the event — handy
    # signal on the card ("3 people involved").
    assignee_count = db.scalar(
        select(func.count(func.distinct(bug_assignees.c.user_id)))
        .select_from(bug_assignees)
        .join(Bug, Bug.id == bug_assignees.c.bug_id)
        .where(Bug.event_id == ev.id)
    ) or 0
    creator_name = None
    if ev.created_by_user_id:
        u = db.get(User, ev.created_by_user_id)
        creator_name = u.name if u else None
    return {
        "id": ev.id,
        "name": ev.name,
        "description": ev.description,
        "scheduled_for": ev.scheduled_for,
        "created_by_user_id": ev.created_by_user_id,
        "created_by_name": creator_name,
        "item_count": int(item_count),
        "assignee_count": int(assignee_count),
        "created_at": ev.created_at,
        "updated_at": ev.updated_at,
    }


def _bug_to_brief(bug: Bug) -> dict:
    """Same shape routes/bugs.py uses, but kept self-contained here so we
    don't have a circular import."""
    return {
        "id": bug.id,
        "project_id": bug.project_id,
        "project_name": bug.project.name if bug.project else None,
        "title": bug.title,
        "description": bug.description,
        "reporter": {
            "id": bug.reporter.id, "name": bug.reporter.name,
            "email": bug.reporter.email, "role": bug.reporter.role,
        } if bug.reporter else None,
        "assignees": [
            {"id": a.id, "name": a.name, "email": a.email, "role": a.role}
            for a in bug.assignees
        ],
        "item_type": getattr(bug, "item_type", None) or "Bug",
        "status": bug.status,
        "priority": bug.priority,
        "environment": bug.environment,
        "due_date": bug.due_date,
        "event_id": bug.event_id,
        "event_name": bug.event.name if bug.event else None,
        "created_at": bug.created_at,
        "updated_at": bug.updated_at,
        "attachment_count": 0,
        "can_edit": True,
    }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@router.get("", response_model=list[EventOut])
def list_events(
    scheduled_for: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    stmt = select(Event).order_by(
        Event.scheduled_for.desc().nullslast() if hasattr(Event.scheduled_for.desc(), "nullslast")
        else Event.scheduled_for.desc(),
        Event.id.desc(),
    )
    if scheduled_for:
        stmt = stmt.where(Event.scheduled_for == scheduled_for)
    rows = list(db.scalars(stmt).all())
    return [_event_brief(db, ev) for ev in rows]


# ---------------------------------------------------------------------------
# Detail (event + its items)
# ---------------------------------------------------------------------------
@router.get("/{event_id}", response_model=EventDetail)
def get_event(
    event_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    ev = db.get(Event, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Event not found")
    items_stmt = select(Bug).options(
        selectinload(Bug.project),
        selectinload(Bug.reporter),
        selectinload(Bug.assignees),
        selectinload(Bug.event),
    ).where(Bug.event_id == event_id).order_by(Bug.id.asc())
    items = list(db.scalars(items_stmt).all())
    payload = _event_brief(db, ev)
    payload["items"] = [_bug_to_brief(b) for b in items]
    return payload


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
def create_event(
    payload: EventCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    ev = Event(
        name=payload.name,
        description=payload.description,
        scheduled_for=payload.scheduled_for,
        created_by_user_id=actor.id,
    )
    db.add(ev)
    db.flush()
    _log(db, ev.id, actor, "event_created",
         f"Event created: {ev.name}"
         + (f" (scheduled for {ev.scheduled_for})" if ev.scheduled_for else ""))
    db.commit()
    db.refresh(ev)
    return _event_brief(db, ev)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
@router.put("/{event_id}", response_model=EventOut)
def update_event(
    event_id: int,
    payload: EventUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    ev = db.get(Event, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Event not found")
    fields = payload.model_dump(exclude_unset=True)
    tracked = ["name", "description", "scheduled_for"]
    changes: list[tuple[str, str, str]] = []
    for f in tracked:
        if f in fields and getattr(ev, f) != fields[f]:
            changes.append((f, str(getattr(ev, f) or ""), str(fields[f] or "")))
    for k, v in fields.items():
        setattr(ev, k, v)
    if changes:
        for field, old, new in changes:
            _log(db, ev.id, actor, f"event_{field}_changed",
                 f"{field}: '{old}' → '{new}'")
        db.commit()
    else:
        db.rollback()
    db.refresh(ev)
    return _event_brief(db, ev)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
@router.delete("/{event_id}")
def delete_event(
    event_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict[str, str]:
    ev = db.get(Event, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Event not found")
    if actor.role not in ("admin", "manager"):
        raise HTTPException(
            status_code=403,
            detail="Only admins and managers can delete events.",
        )
    name = ev.name
    # Items keep existing — the FK is declared with ondelete='SET NULL' but
    # we also explicitly null them here so SQLite (which doesn't always
    # honour the FK ondelete with declarative-style attached relationships
    # on a populated session) does the right thing.
    db.query(Bug).filter(Bug.event_id == event_id).update(
        {Bug.event_id: None}, synchronize_session=False,
    )
    db.delete(ev)
    _log(db, None, actor, "event_deleted",
         f"Deleted event #{event_id}: {name}")
    db.commit()
    return {"message": "Event deleted"}
