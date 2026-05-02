"""Users API. Admin-only for create / update / delete."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    get_current_user, hash_password, invalidate_outstanding_reset_tokens,
    require_admin,
)
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


def _like_escape(needle: str) -> str:
    """Escape LIKE wildcards so `%` and `_` match the literal characters
    rather than 'any'. Pair with escape='\\\\' on the LIKE clause."""
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


@router.get("", response_model=list[UserOut])
def list_users(
    include_inactive: bool = Query(default=True),
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[User]:
    stmt = select(User)
    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))
    if q:
        like = f"%{_like_escape(q.lower())}%"
        stmt = stmt.where(or_(
            func.lower(User.name).like(like, escape="\\"),
            func.lower(User.email).like(like, escape="\\"),
            func.lower(User.role).like(like, escape="\\"),
        ))
    stmt = stmt.order_by(func.lower(User.name))
    return list(db.scalars(stmt).all())


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> User:
    user = User(
        name=payload.name,
        email=payload.email,
        role=payload.role,
        is_active=payload.is_active,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already exists") from exc
    _audit(db, actor, "user_created", user.id,
           f"Created user '{user.name}' <{user.email}> ({user.role})")
    db.commit()
    db.refresh(user)
    return user


@router.get("/{user_id}", response_model=UserOut)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.put("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    fields = payload.model_dump(exclude_unset=True)
    new_password = fields.pop("password", None)
    changes = []

    # Guardrail: don't let admins demote/disable themselves into a corner.
    if actor.id == user_id:
        if "role" in fields and fields["role"] != "admin":
            raise HTTPException(status_code=400, detail="You cannot demote yourself from admin")
        if fields.get("is_active") is False:
            raise HTTPException(status_code=400, detail="You cannot deactivate yourself")

    # Guardrail: don't allow demoting/disabling the last admin.
    will_be_role = fields.get("role", user.role)
    will_be_active = fields.get("is_active", user.is_active)
    if user.role == "admin" and (will_be_role != "admin" or not will_be_active):
        n_other_admins = db.scalar(
            select(func.count(User.id))
            .where(User.role == "admin", User.is_active.is_(True), User.id != user_id)
        ) or 0
        if n_other_admins == 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot remove the last admin. Promote another user first.",
            )

    # If the admin is deactivating someone, kick their existing sessions.
    if fields.get("is_active") is False and user.is_active:
        user.session_version = (user.session_version or 0) + 1

    for key, value in fields.items():
        old = getattr(user, key)
        if old != value:
            changes.append(f"{key}: {old!r} → {value!r}")
            setattr(user, key, value)

    if new_password:
        user.password_hash = hash_password(new_password)
        # An admin password-reset is a security event — kick all existing
        # sessions for this user and revoke their reset tokens too.
        user.session_version = (user.session_version or 0) + 1
        invalidate_outstanding_reset_tokens(db, user.id)
        changes.append("password reset by admin")

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already exists") from exc

    if changes:
        _audit(db, actor, "user_updated", user.id,
               f"Updated user '{user.name}': " + "; ".join(changes))
    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> dict[str, str]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if actor.id == user_id:
        raise HTTPException(status_code=400, detail="You cannot delete yourself")

    if user.role == "admin":
        n_other_admins = db.scalar(
            select(func.count(User.id))
            .where(User.role == "admin", User.is_active.is_(True), User.id != user_id)
        ) or 0
        if n_other_admins == 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete the last admin. Promote another user first.",
            )

    label = f"{user.name} <{user.email}>"
    db.delete(user)
    _audit(db, actor, "user_deleted", user_id, f"Deleted user {label}")
    db.commit()
    return {"message": "User deleted"}
