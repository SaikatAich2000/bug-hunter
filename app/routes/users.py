"""Users API."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Activity, User
from app.schemas import UserIn, UserOut, UserUpdate

router = APIRouter(prefix="/api/users", tags=["users"])


def _audit(db: Session, actor: User | None, action: str, entity_id: int, detail: str) -> None:
    db.add(Activity(
        bug_id=None, entity_type="user", entity_id=entity_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action, detail=detail,
    ))


def _resolve_actor(db: Session, actor_user_id: Optional[int]) -> User | None:
    if actor_user_id is None: return None
    return db.get(User, actor_user_id)


@router.get("", response_model=list[UserOut])
def list_users(
    include_inactive: bool = Query(default=True),
    q: Optional[str] = None,
    db: Session = Depends(get_db),
) -> list[User]:
    stmt = select(User)
    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(
            func.lower(User.name).like(like),
            func.lower(User.email).like(like),
            func.lower(User.role).like(like),
        ))
    stmt = stmt.order_by(func.lower(User.name))
    return list(db.scalars(stmt).all())


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserIn,
    actor_user_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
) -> User:
    user = User(**payload.model_dump())
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already exists") from exc
    actor = _resolve_actor(db, actor_user_id)
    _audit(db, actor, "user_created", user.id,
           f"Created user '{user.name}' <{user.email}> ({user.role or 'no role'})")
    db.commit()
    db.refresh(user)
    return user


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db)) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.put("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    actor_user_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    fields = payload.model_dump(exclude_unset=True)
    changes = []
    for key, value in fields.items():
        old = getattr(user, key)
        if old != value:
            changes.append(f"{key}: {old!r} → {value!r}")
            setattr(user, key, value)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already exists") from exc
    if changes:
        actor = _resolve_actor(db, actor_user_id)
        _audit(db, actor, "user_updated", user.id,
               f"Updated user '{user.name}': " + "; ".join(changes))
    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    actor_user_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    actor = _resolve_actor(db, actor_user_id)
    label = f"{user.name} <{user.email}>"
    db.delete(user)
    _audit(db, actor, "user_deleted", user_id, f"Deleted user {label}")
    db.commit()
    return {"message": "User deleted"}
