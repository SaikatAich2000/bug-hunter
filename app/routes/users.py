"""Users API.

Permissions:
  - List / Read : any authenticated user (so they can be picked as
                  assignees / reporters in bug forms).
  - Create / Update : admin or manager.
  - Delete : admin only.

Role-change and deactivation safety rules:
  - Only admins can grant or revoke the admin role. A manager creating
    a user can pick role=user or role=manager only.
  - You cannot demote / deactivate / delete yourself into a corner.
  - You cannot demote / deactivate / delete the last remaining admin.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import (
    accessible_project_ids, project_ids_for_user, set_user_projects,
)
from app.auth import (
    get_current_user, hash_password, invalidate_outstanding_reset_tokens,
    require_admin, require_manager_or_admin,
)
from app.database import get_db
from app.models import Activity, Project, User, user_projects
from app.password_breach import is_password_breached
from app.schemas import UserIn, UserOut, UserUpdate


def _reject_if_breached(plain: str) -> None:
    """Reject any password that appears in the HIBP corpus. Duplicated from
    routes/auth.py to avoid a circular import between the two route modules."""
    if is_password_breached(plain):
        raise HTTPException(
            status_code=400,
            detail="This password appears in a known breach corpus. "
                   "Please choose a different one.",
        )

router = APIRouter(prefix="/api/users", tags=["users"])


_DETAIL_USER_NOT_FOUND = "User not found"
_DETAIL_EMAIL_EXISTS = "Email already exists"

def _audit(db: Session, actor: User | None, action: str, entity_id: int, detail: str) -> None:
    db.add(Activity(
        bug_id=None, entity_type="user", entity_id=entity_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action, detail=detail,
    ))


def _like_escape(needle: str) -> str:
    """Escape LIKE wildcards so `%` and `_` match the literal characters.
    Pair with escape='\\\\' on the LIKE clause."""
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


def _email_in_use(db: Session, email: str, exclude_id: Optional[int] = None) -> bool:
    """Case-insensitive email-uniqueness check. The API lowercases emails before
    insert, but this also rejects a collision with any mixed-case row the
    exact-match unique constraint would otherwise miss."""
    stmt = select(User.id).where(func.lower(User.email) == email.lower())
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return db.scalar(stmt) is not None


def _validate_assignable_projects(db: Session, actor: User, project_ids: list[int]) -> None:
    """Validate a set of project tags an actor wants to grant a user.

    Every id must reference an existing project. A non-admin actor (a manager)
    can only grant projects they themselves can access — otherwise a
    project-restricted manager could hand out (or, by editing, peek at the
    existence of) projects outside their own scope. An admin may grant any."""
    if not project_ids:
        return
    existing = set(db.scalars(
        select(Project.id).where(Project.id.in_(project_ids))
    ).all())
    missing = [pid for pid in project_ids if pid not in existing]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown project ids: {sorted(missing)}")
    accessible = accessible_project_ids(db, actor)
    if accessible is not None:
        outside = [pid for pid in project_ids if pid not in accessible]
        if outside:
            raise HTTPException(
                status_code=403,
                detail="You can only assign projects you have access to.",
            )


def _user_out(db: Session, user: User) -> dict:
    """Serialize a user for the API including their project tags."""
    return {
        "id": user.id, "name": user.name, "email": user.email,
        "role": user.role, "is_active": user.is_active,
        "created_at": user.created_at, "updated_at": user.updated_at,
        "project_ids": project_ids_for_user(db, user.id),
    }


def _projects_by_user(db: Session, user_ids: list[int]) -> dict[int, list[int]]:
    """Batch map {user_id: [project_id, ...]} for a page of users — one query
    instead of one per row."""
    if not user_ids:
        return {}
    rows = db.execute(
        select(user_projects.c.user_id, user_projects.c.project_id)
        .where(user_projects.c.user_id.in_(user_ids))
        .order_by(user_projects.c.project_id)
    ).all()
    out: dict[int, list[int]] = {}
    for uid, pid in rows:
        out.setdefault(int(uid), []).append(int(pid))
    return out


@router.get("", response_model=list[UserOut])
def list_users(
    include_inactive: bool = Query(default=True),
    q: Optional[str] = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    stmt = select(User)
    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))
    if q:
        # Search name and email only. Folding User.role into the LIKE would turn
        # this picker (available to every authenticated user) into a
        # role-enumeration oracle, e.g. q="admin" returning every admin account.
        # Role-based filtering, if ever needed, should be a param guarded for
        # managers/admins.
        like = f"%{_like_escape(q.lower())}%"
        stmt = stmt.where(or_(
            func.lower(User.name).like(like, escape="\\"),
            func.lower(User.email).like(like, escape="\\"),
        ))
    # Row ceiling so this picker list can't grow unbounded; the users table is
    # bounded by team size, so 500 is well above any realistic count.
    stmt = stmt.order_by(func.lower(User.name)).limit(500)
    rows = list(db.scalars(stmt).all())
    # Attach each user's project tags in one batch query (not N+1). The user list
    # itself is NOT project-scoped: managers still need to see everyone to pick
    # assignees/reporters, exactly as before.
    proj_map = _projects_by_user(db, [u.id for u in rows])
    return [
        {
            "id": u.id, "name": u.name, "email": u.email, "role": u.role,
            "is_active": u.is_active, "created_at": u.created_at,
            "updated_at": u.updated_at, "project_ids": proj_map.get(u.id, []),
        }
        for u in rows
    ]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> dict:
    # Managers can create users but can't grant the admin role; only admins can.
    if payload.role == "admin" and actor.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admins can create admin users.",
        )
    # Reject an initial password that appears in HIBP.
    _reject_if_breached(payload.password)
    # Validate the optional project tags before creating anything (existence +
    # the creator's own access), so a bad/forbidden id is a clean 400/403.
    _validate_assignable_projects(db, actor, payload.project_ids)
    if _email_in_use(db, payload.email):
        raise HTTPException(status_code=409, detail=_DETAIL_EMAIL_EXISTS)
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
        raise HTTPException(status_code=409, detail=_DETAIL_EMAIL_EXISTS) from exc
    if payload.project_ids:
        set_user_projects(db, user.id, payload.project_ids)
    proj_note = (f"; projects: {sorted(payload.project_ids)}"
                 if payload.project_ids else "")
    _audit(db, actor, "user_created", user.id,
           f"Created user '{user.name}' <{user.email}> ({user.role}){proj_note}")
    db.commit()
    db.refresh(user)
    return _user_out(db, user)


@router.get("/{user_id}", response_model=UserOut)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=_DETAIL_USER_NOT_FOUND)
    return _user_out(db, user)


def _check_manager_role_limits(actor: User, target: User, fields: dict) -> None:
    """Managers can't edit admins and can't grant admin. Admins skip this."""
    if actor.role == "admin":
        return
    if target.role == "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admins can edit admin accounts.",
        )
    if "role" in fields and fields["role"] == "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admins can grant the admin role.",
        )


def _check_self_edit_guardrails(actor: User, target_id: int, fields: dict) -> None:
    if actor.id != target_id:
        return
    if "role" in fields and fields["role"] != "admin":
        raise HTTPException(status_code=400, detail="You cannot demote yourself from admin")
    if fields.get("is_active") is False:
        raise HTTPException(status_code=400, detail="You cannot deactivate yourself")


def _count_other_active_admins(db: Session, exclude_id: int) -> int:
    """Count active admins other than `exclude_id`, holding a row lock on the
    admin rows so the check can't race itself.

    Two admins concurrently deleting/demoting each other would each, with a
    plain COUNT, see "1 other admin" and both commit, leaving the org with zero
    admins. SELECT ... FOR UPDATE over all active admins (not just the others)
    makes the second transaction block until the first commits, then re-read the
    smaller set. FOR UPDATE is a real lock on Postgres and a no-op on SQLite.
    """
    admin_ids = db.scalars(
        select(User.id)
        .where(User.role == "admin", User.is_active.is_(True))
        .with_for_update()
    ).all()
    return sum(1 for aid in admin_ids if aid != exclude_id)


def _check_last_admin_guardrail(db: Session, target: User, target_id: int, fields: dict) -> None:
    """Don't allow demoting/disabling the last admin."""
    will_be_role = fields.get("role", target.role)
    will_be_active = fields.get("is_active", target.is_active)
    if target.role != "admin" or (will_be_role == "admin" and will_be_active):
        return
    if _count_other_active_admins(db, target_id) == 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot remove the last admin. Promote another user first.",
        )


def _apply_user_field_changes(user: User, fields: dict, changes: list[str]) -> None:
    """Set every changed field on the user, recording the diff."""
    for key, value in fields.items():
        old = getattr(user, key)
        if old != value:
            changes.append(f"{key}: {old!r} → {value!r}")
            setattr(user, key, value)


def _apply_project_tag_changes(db: Session, user_id: int,
                               new_project_ids: Optional[list[int]],
                               changes: list[str]) -> None:
    """Replace a user's project memberships when the update supplied a new set.

    None = the field was omitted, leave memberships untouched. A list (including
    an empty one, which clears all tags) replaces them. Records the diff for the
    audit line and is a no-op when the set is unchanged."""
    if new_project_ids is None:
        return
    old = set(project_ids_for_user(db, user_id))
    new = set(new_project_ids)
    if old == new:
        return
    set_user_projects(db, user_id, new_project_ids)
    changes.append(f"projects: {sorted(old)} → {sorted(new)}")


def _apply_admin_password_reset(user: User, db: Session, new_password: str,
                                changes: list[str]) -> None:
    """An admin password-reset is a security event: kick existing sessions and
    revoke any outstanding reset tokens."""
    # HIBP check before committing; admin-driven resets aren't exempt since the
    # user might keep the assigned password.
    _reject_if_breached(new_password)
    user.password_hash = hash_password(new_password)
    user.session_version = (user.session_version or 0) + 1
    invalidate_outstanding_reset_tokens(db, user.id)
    changes.append("password reset by admin")


@router.put("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=_DETAIL_USER_NOT_FOUND)

    fields = payload.model_dump(exclude_unset=True)
    new_password = fields.pop("password", None)
    # project_ids isn't a User column — pop it out before the generic field
    # applier (which setattr()s every remaining key). None = leave memberships
    # unchanged; a list (incl. empty) = replace them.
    new_project_ids = fields.pop("project_ids", None)
    changes: list[str] = []

    _check_manager_role_limits(actor, user, fields)
    _check_self_edit_guardrails(actor, user_id, fields)
    _check_last_admin_guardrail(db, user, user_id, fields)
    if new_project_ids is not None:
        _validate_assignable_projects(db, actor, new_project_ids)

    new_email = fields.get("email")
    if new_email and _email_in_use(db, new_email, exclude_id=user_id):
        raise HTTPException(status_code=409, detail=_DETAIL_EMAIL_EXISTS)

    # When deactivating a user, kick their existing sessions.
    if fields.get("is_active") is False and user.is_active:
        user.session_version = (user.session_version or 0) + 1

    _apply_user_field_changes(user, fields, changes)
    _apply_project_tag_changes(db, user_id, new_project_ids, changes)

    if new_password:
        _apply_admin_password_reset(user, db, new_password, changes)

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_DETAIL_EMAIL_EXISTS) from exc

    if changes:
        _audit(db, actor, "user_updated", user.id,
               f"Updated user '{user.name}': " + "; ".join(changes))
    db.commit()
    db.refresh(user)
    return _user_out(db, user)


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> dict[str, str]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=_DETAIL_USER_NOT_FOUND)

    if actor.id == user_id:
        raise HTTPException(status_code=400, detail="You cannot delete yourself")

    if user.role == "admin" and _count_other_active_admins(db, user_id) == 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the last admin. Promote another user first.",
        )

    label = f"{user.name} <{user.email}>"
    db.delete(user)
    _audit(db, actor, "user_deleted", user_id, f"Deleted user {label}")
    db.commit()
    return {"message": "User deleted"}
