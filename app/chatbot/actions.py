"""Sleuth actions — the write-side of the assistant.

Where executor.py is read-only (queries, exports, summaries), this module
performs MUTATIONS the user requested in natural language. Every action:

  1. Re-validates the actor's permission against the same rules used by
     the REST API. The chat path is not a back door.
  2. Applies the change in a single short transaction.
  3. Writes an Activity row so the audit log knows who did what — exactly
     like the REST routes do.
  4. Returns a Response of blocks the router can serialize back to the
     user.

The supported ActionKind list is deliberately conservative. We ship the
operations that map cleanly to single-bug edits and comments. Anything
destructive (delete bug, delete user, password reset, role change) is NOT
exposed via chat — those stay UI-only for the foreseeable future, where
the user clicks through an explicit confirm dialog.

Confirmation flow:
  - Risky writes (status change to Closed, assignee changes, due-date
    changes) are STAGED, not executed, on the first turn. The handler
    returns a Response containing a "confirm" block. The frontend renders
    Yes/No buttons. On Yes, the same plan is executed via a
    follow-up message ("yes" / "confirm") which the router resolves by
    calling memory.take_pending() and dispatching to apply_pending().
  - Safe writes (add comment, set priority on a low-priority bug) can be
    configured to skip the confirm. They are all kept confirmable for now —
    an accidental write from a misparse is far more annoying than one
    extra click.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Literal, Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app import notification_service
from app.auth import (
    can_manage_projects, can_edit_bug, ROLE_ADMIN,
)
from app.models import Activity, Bug, Comment, Project, User
from app.schemas import ALLOWED_ENVIRONMENTS, ALLOWED_PRIORITIES, statuses_for_type

from app.chatbot.executor import Block, Response

# Fallback label when an assign/unassign plan carries ids but no display names.
_UNKNOWN_USERS = "user(s)"


# ---------------------------------------------------------------------------
# ActionPlan — a fully-resolved write request awaiting execution
# ---------------------------------------------------------------------------
@dataclass
class ActionPlan:
    """A concrete change to make. Built once during parse, executed once
    on confirm. Storing IDs (not objects) means it can be serialized into
    memory.store between turns without holding ORM instances across a
    session boundary."""
    kind: Literal[
        "assign", "unassign", "set_status", "set_priority",
        "set_environment", "set_due_date", "add_comment",
        "create_bug", "create_project",
    ]
    actor_user_id: int
    bug_id: Optional[int] = None
    # Bulk target: when non-empty, the action applies to EVERY id here (e.g.
    # "assign all the bugs to Alice"). bug_id stays None for a bulk plan.
    bug_ids: list[int] = field(default_factory=list)
    target_user_ids: list[int] = field(default_factory=list)
    target_user_names: list[str] = field(default_factory=list)
    new_value: Optional[str] = None      # status / priority / env / due_date
    comment_body: Optional[str] = None
    new_title: Optional[str] = None
    new_description: Optional[str] = None
    new_project_id: Optional[int] = None
    new_project_name: Optional[str] = None
    # Human-readable summary, shown in the confirm prompt and the
    # post-execution success message.
    summary_human: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise for memory.store."""
        return {
            "kind": self.kind,
            "actor_user_id": self.actor_user_id,
            "bug_id": self.bug_id,
            "bug_ids": list(self.bug_ids),
            "target_user_ids": list(self.target_user_ids),
            "target_user_names": list(self.target_user_names),
            "new_value": self.new_value,
            "comment_body": self.comment_body,
            "new_title": self.new_title,
            "new_description": self.new_description,
            "new_project_id": self.new_project_id,
            "new_project_name": self.new_project_name,
            "summary_human": self.summary_human,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ActionPlan":
        return cls(
            kind=d.get("kind", ""),
            actor_user_id=d.get("actor_user_id", 0),
            bug_id=d.get("bug_id"),
            bug_ids=list(d.get("bug_ids") or []),
            target_user_ids=list(d.get("target_user_ids") or []),
            target_user_names=list(d.get("target_user_names") or []),
            new_value=d.get("new_value"),
            comment_body=d.get("comment_body"),
            new_title=d.get("new_title"),
            new_description=d.get("new_description"),
            new_project_id=d.get("new_project_id"),
            new_project_name=d.get("new_project_name"),
            summary_human=d.get("summary_human", ""),
        )


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------
def _check_can_edit_bug(actor: User, bug: Bug) -> Optional[str]:
    """Returns None if OK, otherwise an error string.

    Passes the work-item's REAL type so the chat write path enforces the same
    per-type rules as PUT /api/bugs/{id}: regular users may edit Bugs but not
    Tasks/Requirements. Without item_type, can_edit_bug defaults to 'Bug' and
    returns True for everyone, which would let chat silently bypass the REST
    403 (the module is explicitly "not a back door").
    """
    itype = getattr(bug, "item_type", None) or "Bug"
    if not can_edit_bug(actor,
                        bug.reporter_id,
                        [a.id for a in bug.assignees],
                        item_type=itype):
        return f"You don't have permission to edit that {itype.lower()}"
    return None


def _check_can_create_project(actor: User) -> Optional[str]:
    if not can_manage_projects(actor):
        return ("Only admins or managers can create projects. "
                "Ask one of them to do it for you")
    return None


def _check_can_create_bug(actor: User) -> Optional[str]:
    # Per current bug routes: any authenticated user can file a bug.
    # This stays consistent with the REST endpoint POST /api/bugs.
    if not actor.is_active:
        return "Your account is inactive"
    return None


# ---------------------------------------------------------------------------
# Audit helper — mirrors routes/bugs.py::_log
# ---------------------------------------------------------------------------
def _audit(db: Session, bug_id: Optional[int], actor: User,
           action: str, detail: str,
           entity_type: str = "bug", entity_id: Optional[int] = None) -> None:
    db.add(Activity(
        bug_id=bug_id,
        entity_type=entity_type,
        entity_id=entity_id if entity_id is not None else bug_id,
        actor_user_id=actor.id,
        actor_name=actor.name,
        action=action,
        detail=detail,
    ))


# ---------------------------------------------------------------------------
# Block helpers
# ---------------------------------------------------------------------------
def _confirm_response(plan: ActionPlan, prompt: str) -> Response:
    """Build a 'please confirm' response. The frontend renders the
    payload's `prompt` plus Yes / No buttons. On Yes, the user sends
    the literal word 'yes' (or 'confirm') as the next message, which
    the router resolves by calling apply_pending()."""
    return Response(
        blocks=[
            Block("text", {"text": prompt}),
            Block("confirm", {
                "summary": plan.summary_human,
                "yes_label": "Yes, do it",
                "no_label": "Cancel",
            }),
        ],
        summary=f"Awaiting confirmation: {plan.summary_human}",
        intent="confirm_action",
    )


def _success_response(message: str, intent: str = "action_done",
                      bug_id: Optional[int] = None) -> Response:
    blocks: list[Block] = [Block("text", {"text": message})]
    if bug_id is not None:
        blocks.append(Block("suggestions", {
            "items": [
                {"label": f"Show bug #{bug_id}",
                 "send":  f"bug #{bug_id}"},
                {"label": f"Comment on #{bug_id}",
                 "send":  f"comment on #{bug_id}: "},
                {"label": "Recent activity",
                 "send":  "recent activity"},
            ]
        }))
    return Response(blocks=blocks, summary=message[:80], intent=intent)


def _error_response(message: str, intent: str = "action_error") -> Response:
    return Response(
        blocks=[Block("text", {"text": message})],
        summary=message[:80],
        intent=intent,
    )


# ---------------------------------------------------------------------------
# Plan execution — the actual writes
# ---------------------------------------------------------------------------
def _load_bug(db: Session, bug_id: int) -> Optional[Bug]:
    return db.scalar(
        select(Bug)
        .options(selectinload(Bug.project),
                 selectinload(Bug.reporter),
                 selectinload(Bug.assignees))
        .where(Bug.id == bug_id)
    )


def _itype_of(bug: Bug) -> str:
    return (getattr(bug, "item_type", None) or "bug").lower()


def _notify_chat_op(db: Session, bug: Bug, actor: User, *, kind: str,
                    title: str, body: str, extra_user_ids: Iterable[int] = ()) -> None:
    """Write an in-app notification for a Sleuth write, so EVERY operation
    surfaces in the bell. Recipients are the item's stakeholders (reporter +
    assignees) plus the actor and any extras (e.g. just-removed assignees).

    The actor is deliberately INCLUDED (no exclude): the person driving Sleuth
    wants confirmation of what it just did. Rows are added on ``db`` and the
    caller commits them with the change."""
    recipients: set[int] = {actor.id}
    if bug.reporter_id is not None:
        recipients.add(bug.reporter_id)
    recipients.update(a.id for a in bug.assignees)
    recipients.update(extra_user_ids)
    notification_service.notify(
        db, list(recipients), kind=kind, title=title, body=body,
        bug_id=bug.id, actor_name=actor.name,
    )


def _apply_assign(db: Session, plan: ActionPlan, actor: User,
                  notify: bool = True) -> Response:
    bug = _load_bug(db, plan.bug_id) if plan.bug_id else None
    if bug is None:
        return _error_response(f"Bug #{plan.bug_id} not found")
    err = _check_can_edit_bug(actor, bug)
    if err:
        return _error_response(err)
    targets = list(db.scalars(
        select(User).where(User.id.in_(plan.target_user_ids))
    ).all()) if plan.target_user_ids else []
    if not targets:
        return _error_response("Couldn't find the user(s) to assign")

    before = sorted(a.name for a in bug.assignees)
    already = {a.id for a in bug.assignees}
    added = [t for t in targets if t.id not in already]
    new_set = list(bug.assignees) + added
    bug.assignees = new_set
    after = sorted(a.name for a in bug.assignees)
    detail = f"Assignees: {before} -> {after}"
    _audit(db, bug.id, actor, "bug_update", detail)
    # In-app notification (skip on bulk: the bulk caller emits ONE aggregate
    # notification instead of one per item).
    if notify and added:
        names = ", ".join(t.name for t in added)
        _notify_chat_op(
            db, bug, actor, kind="assigned",
            title=f"Assigned to {_itype_of(bug)} #{bug.id}",
            body=f"{actor.name} assigned {names} to “{bug.title}”.",
            extra_user_ids=[t.id for t in added])
    db.commit()
    names = ", ".join(t.name for t in targets)
    return _success_response(
        f"Done — assigned **{names}** to bug #{bug.id} (*{bug.title[:60]}*)",
        bug_id=bug.id,
    )


def _apply_unassign(db: Session, plan: ActionPlan, actor: User,
                    notify: bool = True) -> Response:
    bug = _load_bug(db, plan.bug_id) if plan.bug_id else None
    if bug is None:
        return _error_response(f"Bug #{plan.bug_id} not found")
    err = _check_can_edit_bug(actor, bug)
    if err:
        return _error_response(err)
    drop_ids = set(plan.target_user_ids)
    before = sorted(a.name for a in bug.assignees)
    bug.assignees = [a for a in bug.assignees if a.id not in drop_ids]
    after = sorted(a.name for a in bug.assignees)
    if before == after:
        return _error_response(
            "Nothing changed — those users weren't assigned to this bug"
        )
    detail = f"Assignees: {before} -> {after}"
    _audit(db, bug.id, actor, "bug_update", detail)
    if notify:
        names = ", ".join(plan.target_user_names) or _UNKNOWN_USERS
        _notify_chat_op(
            db, bug, actor, kind="updated",
            title=f"Unassigned from {_itype_of(bug)} #{bug.id}",
            body=f"{actor.name} unassigned {names} from “{bug.title}”.",
            extra_user_ids=drop_ids)
    db.commit()
    names = ", ".join(plan.target_user_names) or _UNKNOWN_USERS
    return _success_response(
        f"Done — removed **{names}** from bug #{bug.id}",
        bug_id=bug.id,
    )


def _validate_field_value(bug: Bug, field_name: str, value: Any) -> Optional[str]:
    """Mirror the REST API's enum/date validation so the chat write path can't
    persist a value the REST PUT would reject (notably a bogus due date)."""
    if field_name == "status":
        if value not in statuses_for_type(_itype_of(bug)):
            return f"“{value}” isn't a valid status for a {_itype_of(bug).lower()}."
        return None
    if field_name == "priority" and value not in ALLOWED_PRIORITIES:
        return f"“{value}” isn't a valid priority."
    if field_name == "environment" and value not in ALLOWED_ENVIRONMENTS:
        return f"“{value}” isn't a valid environment."
    if field_name == "due_date" and value:
        try:
            date.fromisoformat(str(value))
        except ValueError:
            return f"“{value}” isn't a valid date — use YYYY-MM-DD."
    return None


def _apply_set_field(db: Session, plan: ActionPlan, actor: User,
                     field_name: str, label: str, notify: bool = True) -> Response:
    bug = _load_bug(db, plan.bug_id) if plan.bug_id else None
    if bug is None:
        return _error_response(f"Bug #{plan.bug_id} not found")
    err = _check_can_edit_bug(actor, bug)
    if err:
        return _error_response(err)
    old = getattr(bug, field_name)
    new = plan.new_value
    invalid = _validate_field_value(bug, field_name, new)
    if invalid:
        return _error_response(invalid)
    if old == new:
        return _success_response(
            f"Bug #{bug.id} {label} is already **{old}** — nothing to do",
            bug_id=bug.id,
        )
    setattr(bug, field_name, new)
    detail = f"{field_name}: {old!r} -> {new!r}"
    _audit(db, bug.id, actor, "bug_update", detail)
    if notify:
        _notify_chat_op(
            db, bug, actor, kind="updated",
            title=f"{_itype_of(bug).capitalize()} #{bug.id} updated",
            body=f"{actor.name} changed {label} to {new}.")
    db.commit()
    return _success_response(
        f"Done — bug #{bug.id} {label} changed from **{old}** to **{new}**",
        bug_id=bug.id,
    )


def _apply_add_comment(db: Session, plan: ActionPlan, actor: User,
                       notify: bool = True) -> Response:
    bug = _load_bug(db, plan.bug_id) if plan.bug_id else None
    if bug is None:
        return _error_response(f"Bug #{plan.bug_id} not found")
    body = (plan.comment_body or "").strip()
    if not body:
        return _error_response(
            "I don't have any comment text to post. Try: "
            "*comment on #5: this is fixed in commit abc*"
        )
    if len(body) > 4000:
        return _error_response("Comment too long — keep it under 4000 chars")
    c = Comment(bug_id=bug.id, author_user_id=actor.id,
                author_name=actor.name, body=body)
    db.add(c)
    db.flush()
    _audit(db, bug.id, actor, "comment_added",
           f"Comment by {actor.name}: {body[:80]}")
    if notify:
        snippet = body if len(body) < 100 else body[:97] + "..."
        _notify_chat_op(
            db, bug, actor, kind="comment",
            title=f"New comment on {_itype_of(bug)} #{bug.id}",
            body=f"{actor.name}: {snippet}")
    db.commit()
    preview = body if len(body) < 120 else body[:117] + "..."
    return _success_response(
        f"Comment posted on bug #{bug.id}: \"{preview}\"",
        bug_id=bug.id,
    )


def _apply_create_bug(db: Session, plan: ActionPlan, actor: User) -> Response:
    err = _check_can_create_bug(actor)
    if err:
        return _error_response(err)
    title = (plan.new_title or "").strip()
    if not title:
        return _error_response(
            "I need a title to create a bug. Try: "
            "*create a bug titled \"Login broken\" in project Apollo*"
        )
    if len(title) > 200:
        return _error_response("Title too long — keep it under 200 chars")
    project_id = plan.new_project_id
    if project_id is None:
        # No project given — fall back to the first project (matches
        # what the SPA does for "General" project).
        first = db.scalar(select(Project).order_by(Project.id))
        if first is None:
            return _error_response(
                "There are no projects yet. Create one first"
            )
        project_id = first.id
    elif db.get(Project, project_id) is None:
        return _error_response("That project doesn't exist anymore")
    # Mirror the REST POST: never persist an out-of-enum priority. The
    # field-setter path validates; the create path must too.
    priority = (plan.new_value or "Medium")
    if priority not in ALLOWED_PRIORITIES:
        return _error_response(
            f"'{priority}' isn't a valid priority. Allowed: {', '.join(ALLOWED_PRIORITIES)}"
        )
    bug = Bug(
        title=title,
        description=(plan.new_description or ""),
        status="New",
        priority=priority,
        environment="DEV",
        project_id=project_id,
        reporter_id=actor.id,
    )
    db.add(bug)
    db.flush()
    if plan.target_user_ids:
        targets = list(db.scalars(
            select(User).where(User.id.in_(plan.target_user_ids))
        ).all())
        bug.assignees = targets
    _audit(db, bug.id, actor, "bug_create",
           f"Created bug #{bug.id}: {title[:80]}")
    _notify_chat_op(
        db, bug, actor, kind="created",
        title=f"New {_itype_of(bug)} #{bug.id} created",
        body=f"{actor.name} created “{title[:80]}”.")
    db.commit()
    return _success_response(
        f"Created bug #{bug.id} — *{title[:80]}* (you're the reporter)",
        bug_id=bug.id,
    )


def _apply_create_project(db: Session, plan: ActionPlan, actor: User) -> Response:
    err = _check_can_create_project(actor)
    if err:
        return _error_response(err)
    name = (plan.new_project_name or "").strip()
    if not name:
        return _error_response("I need a name to create a project")
    if len(name) > 120:
        return _error_response("Project name too long — keep it under 120 chars")
    # Uniqueness: case-insensitive
    existing = db.scalar(
        select(Project).where(Project.name.ilike(name))
    )
    if existing is not None:
        return _error_response(
            f"There's already a project called **{existing.name}**"
        )
    proj = Project(name=name, description=(plan.new_description or ""))
    db.add(proj)
    db.flush()
    _audit(db, None, actor, "project_create",
           f"Created project '{name}'",
           entity_type="project", entity_id=proj.id)
    # No reporter/assignees on a project — notify the actor so the operation
    # still surfaces in the bell.
    notification_service.notify(
        db, [actor.id], kind="updated",
        title="Project created",
        body=f"{actor.name} created project “{proj.name}”.",
        actor_name=actor.name)
    db.commit()
    return Response(
        blocks=[Block("text", {"text":
            f"Project **{proj.name}** created — you can now file bugs against it"})],
        summary=f"Created project {proj.name}",
        intent="action_done",
    )


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------
def sleuth_write_denied(actor: User) -> Optional[str]:
    """Return a refusal string if this actor may NOT perform write/edit actions
    through Sleuth, else None.

    Policy: Sleuth is a full read/write/edit assistant for ADMINS only
    (everything except delete — Sleuth has no delete action at all). Managers
    and regular users get read/lookup access only; they still edit through the
    app where their role allows, but the assistant won't mutate data for them.
    """
    if actor.role == ROLE_ADMIN:
        return None
    return (
        "I can look things up for you — search bugs, run reports, show details, "
        "counts and activity — but making changes through me is limited to "
        "admins. Please use the app (where your role allows) or ask an admin."
    )


def _dispatch_single(plan: ActionPlan, db: Session, actor: User,
                     notify: bool = True) -> Response:
    """Apply one single-bug (or no-bug) action and return its Response.

    ``notify`` lets the bulk caller suppress per-item notifications (it emits a
    single aggregate one instead)."""
    if plan.kind == "assign":
        return _apply_assign(db, plan, actor, notify=notify)
    if plan.kind == "unassign":
        return _apply_unassign(db, plan, actor, notify=notify)
    if plan.kind == "set_status":
        return _apply_set_field(db, plan, actor, "status", "status", notify=notify)
    if plan.kind == "set_priority":
        return _apply_set_field(db, plan, actor, "priority", "priority", notify=notify)
    if plan.kind == "set_environment":
        return _apply_set_field(db, plan, actor, "environment", "environment", notify=notify)
    if plan.kind == "set_due_date":
        return _apply_set_field(db, plan, actor, "due_date", "due date", notify=notify)
    if plan.kind == "add_comment":
        return _apply_add_comment(db, plan, actor, notify=notify)
    if plan.kind == "create_bug":
        return _apply_create_bug(db, plan, actor)
    if plan.kind == "create_project":
        return _apply_create_project(db, plan, actor)
    return _error_response(f"Unknown action: {plan.kind}")


def _notify_bulk(db: Session, plan: ActionPlan, actor: User, updated: int) -> None:
    """One aggregate in-app notification for a bulk op (not N per item)."""
    if plan.kind in ("assign", "unassign"):
        names = ", ".join(plan.target_user_names) or _UNKNOWN_USERS
        verb = "assigned" if plan.kind == "assign" else "unassigned"
        prep = "to" if plan.kind == "assign" else "from"
        recipients = [actor.id, *plan.target_user_ids]
        title = f"{updated} items — {verb}"
        body = f"{actor.name} {verb} {names} {prep} {updated} items."
        kind = "assigned" if plan.kind == "assign" else "updated"
    else:  # set_status / set_priority
        label = "status" if plan.kind == "set_status" else "priority"
        recipients = [actor.id]
        title = f"{updated} items updated"
        body = f"{actor.name} set {label} to {plan.new_value} on {updated} items."
        kind = "updated"
    notification_service.notify(
        db, recipients, kind=kind, title=title, body=body, actor_name=actor.name)


def _apply_bulk(plan: ActionPlan, db: Session, actor: User) -> Response:
    """Apply a plan's action to EVERY id in plan.bug_ids, aggregating the
    outcome. Each item is committed independently (mirrors the REST bulk
    endpoint), so one bad row never rolls back the rest. Per-item notifications
    are suppressed in favor of ONE aggregate bell entry."""
    updated = skipped = 0
    for bid in plan.bug_ids:
        one = ActionPlan(
            kind=plan.kind, actor_user_id=actor.id, bug_id=bid,
            target_user_ids=list(plan.target_user_ids),
            target_user_names=list(plan.target_user_names),
            new_value=plan.new_value, comment_body=plan.comment_body,
        )
        resp = _dispatch_single(one, db, actor, notify=False)
        if resp.intent == "action_done":
            updated += 1
        else:
            skipped += 1
    if updated:
        _notify_bulk(db, plan, actor, updated)
        db.commit()
    tail = f", {skipped} skipped" if skipped else ""
    return _success_response(
        f"Done — {plan.summary_human}: **{updated}** updated{tail}.",
        intent="action_done",
    )


def execute_plan(plan: ActionPlan, db: Session, actor: User) -> Response:
    """Run a confirmed plan. Caller is responsible for verifying that the
    plan really came from this user (we still re-check actor.id below)."""
    if plan.actor_user_id != actor.id:
        return _error_response("That action was staged for a different user")
    # The admin-only Sleuth-write policy is enforced UPSTREAM in the executor
    # handlers (a non-admin never gets a plan staged, so never reaches here
    # through the chat flow). The per-action _check_can_edit_bug /
    # _check_can_create_bug below re-authorize the REST permission — the real
    # security boundary — at execute time for any direct caller, so a demoted
    # actor can still only do what their current role allows.
    try:
        if plan.bug_ids:
            return _apply_bulk(plan, db, actor)
        return _dispatch_single(plan, db, actor)
    except (SQLAlchemyError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Roll back so a partial change never sticks. Rollback itself may
        # raise if the session is already broken — best-effort either way.
        try:
            db.rollback()
        except SQLAlchemyError:
            pass
        return _error_response(f"Action failed: {exc}")


# ---------------------------------------------------------------------------
# Confirmation-prompt builder — used by the parser layer when staging an
# action that needs user "yes" before it goes ahead.
# ---------------------------------------------------------------------------
def stage_with_confirm(plan: ActionPlan) -> Response:
    """Return a confirm-Response for the staged plan. The caller is
    expected to have already saved the plan into memory.store under the
    actor's user id, so a "yes" follow-up can pop it back out."""
    prompt = (
        f"Just to confirm: **{plan.summary_human}**\n\n"
        f"Reply **yes** (or click below) to proceed, **no** to cancel"
    )
    return _confirm_response(plan, prompt)


__all__ = [
    "ActionPlan",
    "execute_plan",
    "stage_with_confirm",
]
