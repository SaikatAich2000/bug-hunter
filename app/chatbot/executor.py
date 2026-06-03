"""Sleuth executor — turns a ParsedQuery into a structured response.

This module is the only place chat-driven SQL gets built. It is
**deliberately read-only**: the executor never issues an INSERT, UPDATE or
DELETE — it accepts a parsed intent, runs the appropriate SELECT, and
returns a `Response` dataclass for the router to serialise.

The same response structure is used for every intent so the frontend
just renders a uniform list of "blocks":

  - text     — markdown-ish prose
  - table    — header + rows, rendered inline in the chat
  - file     — a server-generated download (Excel) the user can save

Why structured blocks instead of HTML? Two reasons. First, the frontend
escapes everything before rendering, which sidesteps stored-XSS via a
malicious bug title — only known-safe formatting (bold, code, links) is
rendered. Second, this gives the LLM passthrough (router.py) a clean
target shape to emit, so rule-engine and LLM responses look identical.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Activity,
    Attachment,
    Bug,
    Project,
    User,
    bug_assignees,
)

from .nlu import (
    Context,
    ParsedQuery,
    OPEN_STATUSES,
    describe_filters,
    parse,
)


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------
@dataclass
class Block:
    """One renderable chunk of the assistant's reply."""
    kind: str   # "text" | "table" | "file" | "suggestions"
    payload: dict


@dataclass
class Response:
    blocks: list[Block] = field(default_factory=list)
    # The frontend uses this to drive a one-line aria-live announcement.
    summary: str = ""
    # Lightweight observability — surfaces in the network log and helps
    # debug missed intents during dogfooding.
    intent: str = ""
    # When the rule engine wasn't sure, the router can choose to hand off
    # to the optional LLM. This flag isn't shown to the user.
    fallback_eligible: bool = False


# ---------------------------------------------------------------------------
# Context loader — pulls just enough of the DB for the NLU to resolve names.
# A single call costs two cheap SELECTs; we don't cache because users /
# projects are small (hundreds at most for an internal tool) and because
# stale name resolution would be a confusing bug.
# ---------------------------------------------------------------------------
def build_context(db: Session) -> Context:
    users = list(db.scalars(select(User)).all())
    projects = list(db.scalars(select(Project)).all())

    user_tuples: list[tuple[int, str, str, str]] = []
    role_map: dict[int, str] = {}
    for u in users:
        norm_name = (u.name or "").strip().lower()
        # Email local-part lets users say "ask alice" when alice's full
        # name is "Alice Wong" but her email is "alice@…".
        local = ""
        if u.email and "@" in u.email:
            local = u.email.split("@", 1)[0].strip().lower()
        user_tuples.append((u.id, norm_name, local, u.name))
        role_map[u.id] = u.role
    proj_tuples = [
        (p.id, (p.name or "").strip().lower(), p.name) for p in projects
    ]
    return Context(users=user_tuples, projects=proj_tuples, user_role_map=role_map)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bug_row(b: Bug) -> dict[str, Any]:
    """Single bug as a flat row for the chat table or Excel export.

    Kept minimal on purpose — the chat table is narrow and we don't want
    to render giant payloads inline. Heavy fields (description, comments,
    attachments) are reachable via the bug-detail intent if the user
    explicitly asks.
    """
    return {
        "id": b.id,
        "title": b.title,
        "project": b.project.name if b.project else "",
        "status": b.status,
        "priority": b.priority,
        "environment": b.environment,
        "reporter": b.reporter.name if b.reporter else "",
        "assignees": ", ".join(a.name for a in b.assignees),
        "due_date": b.due_date or "",
        "updated_at": b.updated_at.isoformat() if b.updated_at else "",
        "created_at": b.created_at.isoformat() if b.created_at else "",
    }


def _eager_bug_query():
    """Standard bug select with relationships eager-loaded so the row
    dict above doesn't trigger N+1 round-trips."""
    return select(Bug).options(
        selectinload(Bug.project),
        selectinload(Bug.reporter),
        selectinload(Bug.assignees),
        selectinload(Bug.event),
    )


_CREATED_AT_KEYWORDS = (
    "created", "filed", "reported", "opened", "raised",
    "logged", "submitted", "registered",
)


def _apply_both(stmt, count_stmt, clause):
    """Apply the same WHERE clause to both the select and the count statement."""
    return stmt.where(clause), count_stmt.where(clause)


def _apply_text_search(stmt, count_stmt, needle_raw: str):
    """Build the title/description LIKE clause for the text-search filter."""
    needle = needle_raw.lower().replace("\\", "\\\\")
    needle = needle.replace("%", "\\%").replace("_", "\\_")
    like = f"%{needle}%"
    clause = or_(
        func.lower(Bug.title).like(like, escape="\\"),
        func.lower(Bug.description).like(like, escape="\\"),
    )
    return _apply_both(stmt, count_stmt, clause)


def _apply_time_window(stmt, count_stmt, pq: ParsedQuery):
    """Layer the time-window filter onto the statement pair, picking the
    right timestamp column based on the verbs the user used."""
    msg_l = (pq.raw_message or "").lower()
    use_created = any(k in msg_l for k in _CREATED_AT_KEYWORDS)
    col = Bug.created_at if use_created else Bug.updated_at
    if pq.time_window.start:
        stmt, count_stmt = _apply_both(stmt, count_stmt, col >= pq.time_window.start)
    if pq.time_window.end:
        stmt, count_stmt = _apply_both(stmt, count_stmt, col <= pq.time_window.end)
    return stmt, count_stmt


def _apply_bug_filters(stmt, count_stmt, pq: ParsedQuery):
    """Layer the parsed filters onto a select+count statement pair.

    Returned as a tuple so the caller can run COUNT and SELECT side by
    side (we always want the total even when paginating to a slice).
    """
    if pq.statuses:
        stmt, count_stmt = _apply_both(stmt, count_stmt, Bug.status.in_(pq.statuses))
    if pq.priorities:
        stmt, count_stmt = _apply_both(stmt, count_stmt, Bug.priority.in_(pq.priorities))
    if pq.environments:
        stmt, count_stmt = _apply_both(stmt, count_stmt, Bug.environment.in_(pq.environments))
    if pq.project_ids:
        stmt, count_stmt = _apply_both(stmt, count_stmt, Bug.project_id.in_(pq.project_ids))
    if pq.reporter_ids:
        stmt, count_stmt = _apply_both(stmt, count_stmt, Bug.reporter_id.in_(pq.reporter_ids))
    if pq.assignee_ids:
        stmt, count_stmt = _apply_both(
            stmt, count_stmt, Bug.assignees.any(User.id.in_(pq.assignee_ids)),
        )
    # v3.2.1 — "unassigned" / "no assignee" filter. NOT EXISTS over the
    # bug_assignees composite-PK index, so it's a clean index seek.
    if pq.unassigned:
        stmt, count_stmt = _apply_both(stmt, count_stmt, ~Bug.assignees.any())
    if pq.text_search:
        stmt, count_stmt = _apply_text_search(stmt, count_stmt, pq.text_search)
    if pq.time_window and (pq.time_window.start or pq.time_window.end):
        stmt, count_stmt = _apply_time_window(stmt, count_stmt, pq)
    return stmt, count_stmt


# ---------------------------------------------------------------------------
# Intent handlers
# ---------------------------------------------------------------------------
def _handle_greeting(actor: User) -> Response:
    name_part = f", {actor.name.split()[0]}" if actor and actor.name else ""
    text = (
        f"Hi{name_part}! I'm **Sleuth** 🔍, your Bug Hunter assistant.\n\n"
        "Ask me anything about bugs, projects, users or activity. A few "
        "examples:\n"
        "- *show all open bugs assigned to John*\n"
        "- *export critical bugs in PROD to excel*\n"
        "- *how many bugs were filed this week?*\n"
        "- *bug 42*\n\n"
        "Type **help** for the full list"
    )
    return Response(
        blocks=[Block("text", {"text": text})],
        summary="Sleuth ready",
        intent="greeting",
    )


def _handle_thanks() -> Response:
    return Response(
        blocks=[Block("text", {"text": "Anytime — happy hunting 🐞"})],
        summary="Thanks acknowledged",
        intent="thanks",
    )


def _handle_help() -> Response:
    text = (
        "**Sleuth — what I can do**\n\n"
        "**Find bugs**\n"
        "- *show open bugs assigned to <name>*\n"
        "- *list critical bugs in PROD*\n"
        "- *bugs reported by Alice this week*\n"
        "- *bugs in project Mobile with status closed*\n"
        "- *bugs about \"login crash\"*\n\n"
        "**Counts & stats**\n"
        "- *how many open bugs?*\n"
        "- *total bugs in UAT*\n"
        "- *summary* / *dashboard stats*\n\n"
        "**Lookups**\n"
        "- *bug 42* — open the full details\n"
        "- *list all users* / *list managers* / *list admins*\n"
        "- *list projects*\n\n"
        "**Activity**\n"
        "- *recent activity* / *what happened today?*\n\n"
        "**Export**\n"
        "- Add *to excel* / *as xlsx* / *download* to any list query and "
        "I'll generate a spreadsheet you can save.\n\n"
        "**Take actions** *(I'll always ask before changing anything)*\n"
        "- *close bug 5* / *reopen #12* / *mark bug 7 as resolved*\n"
        "- *assign bug 3 to Alice* / *unassign Bob from #5*\n"
        "- *set bug 9 priority to high* / *make bug 3 critical*\n"
        "- *comment on #5: looks fixed in v2.1*\n"
        "- *due bug 8 2026-06-15*\n"
        "- *create a bug titled \"Login broken\" in project Apollo*\n"
        "- *create project Mercury*  (admin / manager only)\n\n"
        "**Pronouns**\n"
        "After viewing or filtering a bug I remember it for the next "
        "30 minutes — so *close it* or *comment on that bug: ...* both "
        "work.\n\n"
        "I match status synonyms (open / fixed / WIP / blocker / urgent / "
        "P0–P3), environment shortcuts (prod / staging / dev) and time "
        "windows (today, yesterday, this week, last 7 days). I'll ask "
        "for clarification when a name matches more than one person"
    )
    return Response(
        blocks=[Block("text", {"text": text})],
        summary="Help",
        intent="help",
    )


def _handle_about(message: str) -> Response:
    """Cheap explainer for "what is X" questions about the product itself.

    We answer from a small static knowledge base baked into this module
    — no external calls. Anything unrecognised falls back to a polite
    "I'm not sure" with a help nudge.
    """
    msg = message.lower()
    facts = {
        "status": (
            "**Statuses**: New, In Progress, Reopened, Resolved, Closed, "
            "Resolve Later, Not a Bug. *Open* groups New + In Progress + "
            "Reopened. *Not a Bug* is excluded from the total-bugs KPI"
        ),
        "priority": (
            "**Priorities**: Low, Medium, High, Critical. P0 → Critical, "
            "P1 → High, P2 → Medium, P3 → Low when you use those aliases"
        ),
        "priorities": (
            "**Priorities**: Low, Medium, High, Critical"
        ),
        "environment": (
            "**Environments**: DEV, UAT, PROD. *staging*, *qa*, *test* are "
            "treated as UAT; *production*, *live* mean PROD"
        ),
        "role": (
            "**Roles**: admin, manager, user. Admins manage everything. "
            "Managers can edit bugs / projects / non-admin users but can't "
            "delete users or projects. Regular users can create and edit "
            "bugs but can't manage other users"
        ),
        "audit": (
            "The audit trail records every create, update, delete, login, "
            "and session revoke. Visible to admins and managers from the "
            "Audit Trail sidebar item"
        ),
        "session": (
            "Every login creates a server-side session. Admins can see all "
            "active sessions and revoke individual ones from the Sessions "
            "sidebar item. Revoking your own current session isn't allowed "
            "— use Log out instead"
        ),
        "attachment": (
            "Attachments (PDF, image, video — up to 50 MB each) are stored "
            "as BLOBs in PostgreSQL, so a database backup includes every "
            "attachment automatically"
        ),
    }
    for keyword, answer in facts.items():
        if keyword in msg:
            return Response(
                blocks=[Block("text", {"text": answer})],
                summary="Explanation",
                intent="about",
            )
    return Response(
        blocks=[Block("text", {"text":
            "I'm not sure I caught that. Type **help** to see the kinds "
            "of questions I can answer"})],
        summary="Unknown",
        intent="about",
        fallback_eligible=True,
    )


def _handle_unknown(message: str = "") -> Response:
    """Friendly fallback for queries the rule engine couldn't classify.

    v3.2.1 — tries to suggest a closer rephrasing based on tokens we DID
    see. Plain-language echo so the user understands what slipped past
    the parser. Falls back to the original generic hint if no signal.
    """
    msg = (message or "").lower()
    hints: list[str] = []
    # Default hints suggest the canonical bug-search phrasings — they're
    # what most users actually want. We swap in topic-specific hints when
    # the user clearly mentioned users / projects / stats; otherwise we
    # fall through to the bug-shaped default (no need for a separate
    # else-branch with identical body, Sonar S1871).
    if any(w in msg for w in ("user", "users", "team",
                              "member", "members")):
        hints.append("*list users* or *list admins*")
    elif any(w in msg for w in ("project", "projects")):
        hints.append("*list projects*")
    elif any(w in msg for w in ("stat", "stats", "kpi",
                                "summary", "dashboard")):
        hints.append("*summary* or *stats*")
    else:
        # Bug words OR no recognised topic word both get bug-shaped hints.
        hints.append("*show open bugs assigned to <name>*")
        hints.append("*how many critical bugs in PROD?*")
    bullet_lines = "\n".join(f"- {h}" for h in hints[:3])
    return Response(
        blocks=[Block("text", {"text":
            "Hmm, I didn't quite catch that. A few things you can try:\n\n"
            f"{bullet_lines}\n\n"
            "Or type **help** for the full list"})],
        summary="Unknown",
        intent="unknown",
        fallback_eligible=True,
    )


def _handle_list_users(db: Session, pq: ParsedQuery) -> Response:
    stmt = select(User).order_by(func.lower(User.name))
    if pq.role_filter:
        stmt = stmt.where(User.role == pq.role_filter)
    rows = list(db.scalars(stmt).all())
    if not rows:
        return Response(
            blocks=[Block("text", {"text": "No users match that filter"})],
            summary="0 users",
            intent="list_users",
        )
    headers = ["Name", "Email", "Role", "Active"]
    data = [
        [u.name, u.email, u.role, "Yes" if u.is_active else "No"]
        for u in rows
    ]
    label = "users"
    if pq.role_filter:
        label = f"{pq.role_filter}s" if not pq.role_filter.endswith("s") else pq.role_filter
    return Response(
        blocks=[
            Block("text", {"text": f"Found **{len(rows)}** {label}"}),
            Block("table", {"headers": headers, "rows": data}),
        ],
        summary=f"{len(rows)} {label}",
        intent="list_users",
    )


def _handle_list_projects(db: Session) -> Response:
    rows = list(db.scalars(
        select(Project).order_by(func.lower(Project.name))
    ).all())
    if not rows:
        return Response(
            blocks=[Block("text", {"text": "There are no projects yet"})],
            summary="0 projects",
            intent="list_projects",
        )
    # Per-project bug counts in one grouped query.
    counts = dict(db.execute(
        select(Bug.project_id, func.count(Bug.id)).group_by(Bug.project_id)
    ).all())
    headers = ["Name", "Bugs", "Description"]
    data = [
        [p.name, str(int(counts.get(p.id, 0))), p.description or "—"]
        for p in rows
    ]
    return Response(
        blocks=[
            Block("text", {"text": f"Found **{len(rows)}** project(s)"}),
            Block("table", {"headers": headers, "rows": data}),
        ],
        summary=f"{len(rows)} projects",
        intent="list_projects",
    )


def _handle_bug_detail(db: Session, pq: ParsedQuery) -> Response:
    bug = db.scalar(_eager_bug_query().where(Bug.id == pq.bug_id))
    if bug is None:
        return Response(
            blocks=[Block("text", {"text":
                f"I couldn't find a bug with ID **#{pq.bug_id}**. "
                "Maybe it was deleted, or the number is off?"})],
            summary="Not found",
            intent="bug_detail",
        )
    att_count = db.scalar(
        select(func.count(Attachment.id)).where(Attachment.bug_id == bug.id)
    ) or 0
    descr = (bug.description or "").strip()
    short_descr = (descr[:600] + "…") if len(descr) > 600 else descr
    # Surface the work-item TYPE and EVENT in Sleuth's reply so users
    # asking "show me #42" don't lose context. Falls back to "Bug" when
    # the column is missing (legacy rows pre-2.3).
    itype = getattr(bug, "item_type", None) or "Bug"
    event_name = bug.event.name if getattr(bug, "event", None) else None
    body = (
        f"**{itype} #{bug.id} — {bug.title}**\n\n"
        f"**Type:** {itype} · **Status:** {bug.status} · **Priority:** {bug.priority} · "
        f"**Environment:** {bug.environment}\n"
        f"**Project:** {bug.project.name if bug.project else '—'}\n"
    )
    if event_name:
        body += f"**Event:** {event_name}\n"
    body += (
        f"**Reporter:** {bug.reporter.name if bug.reporter else '—'}\n"
        f"**Assignees:** "
        f"{', '.join(a.name for a in bug.assignees) or '—'}\n"
        f"**Due:** {bug.due_date or '—'} · "
        f"**Attachments:** {att_count}\n"
        f"**Created:** {bug.created_at.isoformat() if bug.created_at else '—'}\n"
        f"**Updated:** {bug.updated_at.isoformat() if bug.updated_at else '—'}\n"
    )
    if short_descr:
        body += f"\n**Description:**\n{short_descr}"
    body += (
        f"\n\n[Open in Bug Hunter](#open-bug-{bug.id})"
    )
    return Response(
        blocks=[Block("text", {"text": body, "open_bug_id": bug.id})],
        summary=f"Bug #{bug.id}",
        intent="bug_detail",
    )


def _handle_recent_activity(db: Session, pq: ParsedQuery, actor: User) -> Response:
    # Audit is restricted — non-admin/manager users see only their own
    # bug-related activity. We mirror that boundary here so the chatbot
    # can't be a side door around the global audit policy.
    stmt = select(Activity).order_by(
        Activity.created_at.desc(), Activity.id.desc()
    )
    if actor.role not in ("admin", "manager"):
        # Limit to their own actions OR activities on bugs they reported /
        # are assigned to. Cheaper approximation: just their own actions —
        # bug-level activity for their bugs is reachable from each bug.
        stmt = stmt.where(Activity.actor_user_id == actor.id)
    if pq.time_window:
        if pq.time_window.start:
            stmt = stmt.where(Activity.created_at >= pq.time_window.start)
        if pq.time_window.end:
            stmt = stmt.where(Activity.created_at <= pq.time_window.end)
    stmt = stmt.limit(25)
    rows = list(db.scalars(stmt).all())
    if not rows:
        return Response(
            blocks=[Block("text", {"text":
                "No recent activity"
                + (f" {pq.time_window.label}" if pq.time_window else "")})],
            summary="0 events",
            intent="recent_activity",
        )
    data = [
        [
            r.created_at.isoformat() if r.created_at else "—",
            r.actor_name,
            r.action,
            (r.detail[:160] + "…") if r.detail and len(r.detail) > 160 else (r.detail or ""),
        ]
        for r in rows
    ]
    return Response(
        blocks=[
            Block("text", {"text":
                f"Showing the **{len(rows)}** most recent activity entr"
                f"{'y' if len(rows)==1 else 'ies'}"
                + (f" {pq.time_window.label}" if pq.time_window else "")}),
            Block("table", {
                "headers": ["When", "Actor", "Action", "Detail"],
                "rows": data,
            }),
        ],
        summary=f"{len(rows)} activity entries",
        intent="recent_activity",
    )


def _handle_stats(db: Session) -> Response:
    """Lightweight stats — mirrors the dashboard KPIs.

    Perf v3.2.1: was 7 single-cell COUNT queries plus the top-assignees
    join. Collapsed to 3 GROUP BY scans of the bugs table (status,
    priority, environment) which produce the same numbers but with one
    DB round-trip each instead of one per KPI."""
    excluded = ["Not a Bug"]

    by_status: dict[str, int] = {}
    for s, c in db.execute(
        select(Bug.status, func.count(Bug.id)).group_by(Bug.status)
    ).all():
        by_status[s] = int(c)

    total = sum(c for s, c in by_status.items() if s not in excluded)
    open_n = sum(by_status.get(s, 0) for s in OPEN_STATUSES)
    resolved = by_status.get("Resolved", 0)
    closed = by_status.get("Closed", 0)
    later = by_status.get("Resolve Later", 0)

    crit = int(db.scalar(
        select(func.count(Bug.id)).where(Bug.priority == "Critical")
    ) or 0)
    prod = int(db.scalar(
        select(func.count(Bug.id)).where(Bug.environment == "PROD")
    ) or 0)

    text = (
        f"**Bug Hunter — current snapshot**\n\n"
        f"- **Total** (excluding *Not a Bug*): {total}\n"
        f"- **Open** (New + In Progress + Reopened): {open_n}\n"
        f"- **Resolved**: {resolved}\n"
        f"- **Closed**: {closed}\n"
        f"- **Resolve Later**: {later}\n"
        f"- **Critical**: {crit}\n"
        f"- **In PROD**: {prod}\n"
    )
    # Top 5 assignees with open bugs.
    top = db.execute(
        select(User.name, func.count(bug_assignees.c.bug_id))
        .join(bug_assignees, bug_assignees.c.user_id == User.id)
        .join(Bug, Bug.id == bug_assignees.c.bug_id)
        .where(Bug.status.in_(OPEN_STATUSES))
        .group_by(User.id, User.name)
        .order_by(func.count(bug_assignees.c.bug_id).desc())
        .limit(5)
    ).all()
    blocks = [Block("text", {"text": text})]
    if top:
        blocks.append(Block("table", {
            "headers": ["Assignee", "Open bugs"],
            "rows": [[name, str(int(count))] for name, count in top],
        }))
    return Response(
        blocks=blocks,
        summary=f"{total} bugs, {open_n} open",
        intent="stats",
    )


def _build_user_suggest_pool(ctx: Context) -> dict[str, str]:
    """Pool of (lookup-key -> display-name) for the user-suggestion matcher.
    Keys are normalized names and email local-parts."""
    pool: dict[str, str] = {}
    for _uid, norm_name, email_local, display in ctx.users:
        if norm_name:
            pool.setdefault(norm_name, display)
        if email_local:
            pool.setdefault(email_local, display)
    return pool


def _dedupe_display_names(matches: list[str], pool: dict[str, str]) -> list[str]:
    """Map raw matches back to display names, dropping duplicates while
    preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for m in matches:
        d = pool.get(m)
        if d and d not in seen:
            out.append(d)
            seen.add(d)
    return out


def _suggest_user(phrase: str, ctx: Optional[Context]) -> str:
    """Return a short suggestion string for an unresolved user phrase.

    Uses stdlib difflib to find the closest 1-2 matches against the display
    names and email local-parts already loaded into the parser context.
    Empty string if nothing's close enough. Phrased as a helpful nudge,
    never as a guess we apply on the user's behalf.
    """
    if not ctx or not phrase:
        return ""
    needle = phrase.strip().lower()
    if not needle:
        return ""
    pool = _build_user_suggest_pool(ctx)
    if not pool:
        return ""
    import difflib
    matches = difflib.get_close_matches(needle, list(pool.keys()), n=2, cutoff=0.6)
    suggestions = _dedupe_display_names(matches, pool) if matches else []
    if not suggestions:
        return ""
    if len(suggestions) == 1:
        return f"Did you mean **{suggestions[0]}**?"
    return f"Did you mean **{suggestions[0]}** or **{suggestions[1]}**?"


_LIST_BUGS_HEADERS = ["#", "Title", "Project", "Status", "Priority", "Env",
                      "Reporter", "Assignees", "Due"]


def _clarify_ambiguous_names(pq: ParsedQuery) -> Optional[Response]:
    """Return a clarification Response if the parser saw an ambiguous name,
    otherwise None."""
    if not pq.ambiguous_names:
        return None
    first = pq.ambiguous_names[0]
    names_str = ", ".join(first[1])
    return Response(
        blocks=[Block("text", {"text":
            f"More than one user matches **{first[0]}**: {names_str}. "
            "Could you give the full name (e.g. *assigned to Alice "
            "Wong*)?"})],
        summary="Ambiguous name",
        intent="clarify",
    )


def _clarify_unresolved_user(pq: ParsedQuery, ctx: Optional[Context]) -> Optional[Response]:
    """Return a clarification Response when a named user couldn't be
    resolved; otherwise None. We prefer to stop and ask rather than
    silently drop the filter and return every bug."""
    if not (pq.unresolved_assignee_names or pq.unresolved_reporter_names):
        return None
    role = "assignee" if pq.unresolved_assignee_names else "reporter"
    phrase = (pq.unresolved_assignee_names
              or pq.unresolved_reporter_names)[0]
    suggestion_msg = _suggest_user(phrase, ctx)
    verb = "assigned to" if role == "assignee" else "reported by"
    body = (f"I couldn't find a user named **{phrase}** — so I'm "
            f"not running this as a *{verb}* query. ")
    body += suggestion_msg or (
        "Try the full name, the email local-part, or *list users* to see who exists"
    )
    return Response(
        blocks=[Block("text", {"text": body})],
        summary=f"Unknown {role}",
        intent="clarify",
    )


def _format_bug_row(b: Bug) -> list[str]:
    """One bug → one row of the rendered table."""
    return [
        f"#{b.id}",
        (b.title or "")[:80],
        (b.project.name if b.project else "")[:40],
        b.status,
        b.priority,
        b.environment,
        (b.reporter.name if b.reporter else "")[:40],
        (", ".join(a.name for a in b.assignees))[:60],
        b.due_date or "",
    ]


def _build_export_suggestion_block(pq: ParsedQuery) -> Block:
    """Build the "Export to Excel" suggestion block tacked onto every
    non-empty result. The send text re-uses the original query so the
    parser still has every filter."""
    base = (pq.raw_message or "").strip().rstrip("?.!,;:")
    export_send = f"{base}, export to excel" if base else "export to excel"
    return Block("suggestions", {
        "items": [{"label": "Export to Excel", "send": export_send}],
    })


def _build_count_response(total: int, descr: str) -> Response:
    verb = "is" if total == 1 else "are"
    s = "" if total == 1 else "s"
    return Response(
        blocks=[Block("text", {"text": f"There {verb} **{total}** bug{s} {descr}"})],
        summary=f"{total} bugs",
        intent="count_bugs",
    )


def _build_no_results_response(descr: str) -> Response:
    empty_text = (
        "There are no bugs in the system yet"
        if descr == "(no filters)"
        else f"No bugs found {descr}"
    )
    return Response(
        blocks=[Block("text", {"text": empty_text})],
        summary="0 bugs",
        intent="list_bugs",
    )


def _build_inline_list_response(rows: list[Bug], total: int, limit: int, pq: ParsedQuery, descr: str) -> Response:
    data = [_format_bug_row(b) for b in rows]
    summary_text = f"Found **{total}** bug{'' if total == 1 else 's'} {descr}"
    if total > limit:
        summary_text += f" — showing the most recent {limit}"
    blocks = [
        Block("text", {"text": summary_text}),
        Block("table", {
            "headers": _LIST_BUGS_HEADERS,
            "rows": data,
            "row_bug_ids": [b.id for b in rows],
        }),
        _build_export_suggestion_block(pq),
    ]
    return Response(blocks=blocks, summary=f"{total} bugs", intent="list_bugs")


def _list_bugs_order(pq: ParsedQuery):
    """v3.2.1 — sort hint. "oldest" / "stale" → ASC by updated_at."""
    if pq.sort_oldest:
        return (Bug.updated_at.asc(), Bug.id.asc())
    return (Bug.updated_at.desc(), Bug.id.desc())


def _handle_list_bugs(db: Session, pq: ParsedQuery, ctx: Optional[Context] = None) -> Response:
    """Run the parsed bug filter and render either count / list / file."""
    clarify = _clarify_ambiguous_names(pq) or _clarify_unresolved_user(pq, ctx)
    if clarify is not None:
        return clarify

    stmt, count_stmt = _apply_bug_filters(_eager_bug_query(), select(func.count(Bug.id)), pq)
    total = db.scalar(count_stmt) or 0
    descr = describe_filters(pq) or "(no filters)"

    if pq.wants_count and not pq.wants_export:
        return _build_count_response(total, descr)

    order_cols = _list_bugs_order(pq)

    if pq.wants_export:
        # Hard cap exports so a low-resource box doesn't fall over on
        # "give me everything". The CSV route handles bigger pulls.
        export_cap = 5000
        rows = list(db.scalars(stmt.order_by(*order_cols).limit(export_cap)).all())
        return _build_export_response(rows, pq, total, export_cap)

    limit = max(5, min(pq.limit or 100, 200))
    rows = list(db.scalars(stmt.order_by(*order_cols).limit(limit)).all())
    if total == 0:
        return _build_no_results_response(descr)
    return _build_inline_list_response(rows, total, limit, pq, descr)


def _build_export_response(rows: list[Bug], pq: ParsedQuery, total: int, cap: int) -> Response:
    """Generate the Excel file via excel.py and return a file block."""
    # Lazy import so the chatbot module can be loaded even if openpyxl
    # is missing (e.g. test environments). Failure here is gracefully
    # surfaced as text instead of a 500.
    try:
        from . import excel  # noqa: WPS433
    except ImportError:
        return Response(
            blocks=[Block("text", {"text":
                "Sorry, the Excel exporter isn't available on this server. "
                "Try the CSV export from the sidebar instead"})],
            summary="Excel disabled",
            intent="export_failed",
        )

    descr = describe_filters(pq) or "all bugs"
    file_label = "bugs"
    if pq.assignee_names:
        file_label += "_for_" + "_".join(
            re.sub(r"[^A-Za-z0-9]+", "_", n).strip("_")
            for n in pq.assignee_names
        )
    if pq.statuses:
        file_label += "_" + "_".join(
            re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
            for s in pq.statuses
        )
    file_label = (file_label[:80].strip("_") or "bugs") + ".xlsx"

    try:
        rows_dicts = [_bug_row(b) for b in rows]
        token, byte_size = excel.stage_workbook(rows_dicts, file_label, descr)
    except excel.ExcelGenerationError as exc:
        return Response(
            blocks=[Block("text", {"text":
                f"I couldn't build the spreadsheet: {exc}"})],
            summary="Excel error",
            intent="export_failed",
        )

    note = (
        f"I built a spreadsheet of **{len(rows)}** bug{'' if len(rows)==1 else 's'} "
        f"{descr}"
    )
    if total > cap:
        note += (
            f". The full result has **{total}** bugs — I capped the export "
            f"at {cap} most-recently-updated rows to keep things fast"
        )
    return Response(
        blocks=[
            Block("text", {"text": note}),
            Block("file", {
                "filename": file_label,
                "size_bytes": byte_size,
                "download_token": token,
                "row_count": len(rows),
            }),
        ],
        summary=f"Exported {len(rows)} bugs",
        intent="export_bugs",
    )


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def _resolve_pronouns(pq, actor: User) -> None:
    """If the parser flagged a pronoun reference but couldn't fill in a
    bug id, look up the conversation memory and substitute the last bug
    the user discussed. Mutates pq in place."""
    if pq.bug_id is None and getattr(pq, "used_pronoun_bug", False):
        from app.chatbot.memory import store as _mem
        sess = _mem.get(actor.id)
        if sess is not None and sess.last_bug_id:
            pq.bug_id = sess.last_bug_id


_ACTIONS_NEEDING_BUG = frozenset({
    "assign", "unassign", "set_status", "set_priority",
    "set_environment", "set_due_date", "add_comment",
})


def _plan_assign(plan, pq, kind):
    if not pq.assignee_ids:
        return None, ("I need a name. Try *assign bug 5 to alice* "
                      "or *unassign bob from #5*")
    plan.target_user_ids = list(pq.assignee_ids)
    plan.target_user_names = list(pq.assignee_names)
    verb = "Assign" if kind == "assign" else "Unassign"
    names = ", ".join(plan.target_user_names) or "user(s)"
    direction = "to" if kind == "assign" else "from"
    plan.summary_human = f"{verb} {names} {direction} bug #{pq.bug_id}"
    return plan, None


def _plan_set_status(plan, pq):
    if not pq.action_value:
        return None, ("What status? e.g. *mark bug 5 as resolved* or "
                      "*close bug 5*")
    plan.new_value = pq.action_value
    plan.summary_human = f"Set bug #{pq.bug_id} status to {pq.action_value}"
    return plan, None


def _plan_set_priority(plan, pq):
    if not pq.action_value:
        return None, ("What priority? e.g. *set bug 5 priority to high*")
    plan.new_value = pq.action_value
    plan.summary_human = f"Set bug #{pq.bug_id} priority to {pq.action_value}"
    return plan, None


def _plan_set_environment(plan, pq):
    if not pq.environments:
        return None, ("Which environment? DEV / UAT / PROD")
    plan.new_value = pq.environments[0]
    plan.summary_human = f"Set bug #{pq.bug_id} environment to {plan.new_value}"
    return plan, None


def _plan_set_due_date(plan, pq):
    if not pq.action_value:
        return None, ("What date? Use YYYY-MM-DD format, e.g. "
                      "*due bug 5 2026-06-15*")
    plan.new_value = pq.action_value
    plan.summary_human = f"Set bug #{pq.bug_id} due date to {pq.action_value}"
    return plan, None


def _plan_add_comment(plan, pq):
    if not pq.action_comment:
        return None, ("What should the comment say? Use a colon, e.g. "
                      "*comment on #5: works for me*")
    plan.comment_body = pq.action_comment
    preview = pq.action_comment if len(pq.action_comment) < 60 \
        else pq.action_comment[:57] + "..."
    plan.summary_human = f'Comment on bug #{pq.bug_id}: "{preview}"'
    return plan, None


def _plan_create_bug(plan, pq):
    if not pq.action_title:
        return None, ("I need a title. Try *create a bug titled "
                      '"Login broken" in project Apollo*')
    plan.new_title = pq.action_title
    if pq.priorities:
        plan.new_value = pq.priorities[0]
    if pq.project_ids:
        plan.new_project_id = pq.project_ids[0]
        plan.new_project_name = pq.project_names[0]
    if pq.assignee_ids:
        plan.target_user_ids = list(pq.assignee_ids)
        plan.target_user_names = list(pq.assignee_names)
    proj_part = (f" in project {plan.new_project_name}"
                 if plan.new_project_name else "")
    plan.summary_human = f'Create bug "{pq.action_title[:60]}"{proj_part}'
    return plan, None


def _plan_create_project(plan, pq):
    if not pq.action_title:
        return None, ("I need a project name, e.g. "
                      "*create project Mercury*")
    plan.new_project_name = pq.action_title
    plan.summary_human = f'Create project "{pq.action_title}"'
    return plan, None


def _build_action_plan(pq, actor: User) -> "tuple[Any, Optional[str]]":
    """Translate a parsed write-intent into an ActionPlan.

    Returns (plan, error_message). If the plan can't be built (missing
    target bug, missing assignee, etc.), plan is None and error_message
    is a human string the executor surfaces directly.
    """
    from app.chatbot.actions import ActionPlan
    kind = pq.action_kind
    plan = ActionPlan(kind=kind, actor_user_id=actor.id)

    if kind in _ACTIONS_NEEDING_BUG and pq.bug_id is None:
        if pq.used_pronoun_bug:
            return None, ("I don't know which bug you mean. "
                          "Try mentioning the bug id, e.g. *close bug 5*")
        return None, ("Which bug? Tell me the id — for example "
                      "*close bug 5* or *comment on #12: works for me*")
    plan.bug_id = pq.bug_id

    if kind in ("assign", "unassign"):
        return _plan_assign(plan, pq, kind)
    if kind == "set_status":
        return _plan_set_status(plan, pq)
    if kind == "set_priority":
        return _plan_set_priority(plan, pq)
    if kind == "set_environment":
        return _plan_set_environment(plan, pq)
    if kind == "set_due_date":
        return _plan_set_due_date(plan, pq)
    if kind == "add_comment":
        return _plan_add_comment(plan, pq)
    if kind == "create_bug":
        return _plan_create_bug(plan, pq)
    if kind == "create_project":
        return _plan_create_project(plan, pq)
    return None, f"I don't know how to do '{kind}'"


def _handle_action_request(pq, actor: User) -> Response:
    """Route an action_* intent to plan-building + confirmation staging."""
    from app.chatbot import actions as _actions
    from app.chatbot.memory import store as _mem
    plan, err = _build_action_plan(pq, actor)
    if plan is None:
        return Response(
            blocks=[Block("text", {"text": err})],
            summary=err[:80],
            intent="action_invalid",
        )
    # Stage and ask for confirmation. The router pulls memory back out
    # when the user replies "yes" / "no".
    _mem.stage_pending(actor.id, plan.to_dict())
    return _actions.stage_with_confirm(plan)


def _handle_confirm_yes(db: Session, actor: User) -> Response:
    from app.chatbot import actions as _actions
    from app.chatbot.memory import store as _mem
    raw = _mem.take_pending(actor.id)
    if raw is None:
        return Response(
            blocks=[Block("text", {"text":
                "Nothing to confirm right now. Tell me what to do — for "
                "example *close bug 5* or *assign #12 to alice*"})],
            summary="No pending action",
            intent="confirm_idle",
        )
    plan = _actions.ActionPlan.from_dict(raw)
    resp = _actions.execute_plan(plan, db, actor)
    # Refresh memory with the affected bug so pronouns continue to work.
    if plan.bug_id:
        _mem.remember_bug(actor.id, plan.bug_id)
    return resp


def _handle_confirm_no(actor: User) -> Response:
    from app.chatbot.memory import store as _mem
    raw = _mem.take_pending(actor.id)
    msg = ("Cancelled — I haven't changed anything"
           if raw is not None else
           "Nothing was pending — nothing changed")
    return Response(
        blocks=[Block("text", {"text": msg})],
        summary="Cancelled",
        intent="confirm_cancel",
    )


def _resolve_me_pronoun(pq, actor: User) -> None:
    """v3.2.1 — resolve "me" / "mine" / "I" to the actor's id, splicing into
    the right slot so every downstream handler sees the resolved filter."""
    if not (pq.used_pronoun_me and actor is not None):
        return
    if pq.me_role == "reporter":
        if actor.id not in pq.reporter_ids:
            pq.reporter_ids.append(actor.id)
            pq.reporter_names.append(actor.name)
        return
    if actor.id not in pq.assignee_ids:
        pq.assignee_ids.append(actor.id)
        pq.assignee_names.append(actor.name)


def _empty_intent_response() -> Response:
    return Response(
        blocks=[Block("text", {"text":
            "Type a question to get started — e.g. *open bugs assigned "
            "to me* or *summary*."})],
        summary="Empty input",
        intent="empty",
    )


def _dispatch_read_intent(intent: str, db: Session, pq, actor: User, ctx) -> Optional[Response]:
    """Dispatch a read-side parser intent to its handler.
    Returns None if the intent isn't a known read intent."""
    if intent == "empty":
        return _empty_intent_response()
    if intent == "greeting":
        return _handle_greeting(actor)
    if intent == "thanks":
        return _handle_thanks()
    if intent == "help":
        return _handle_help()
    if intent == "about":
        return _handle_about(pq.raw_message or "")
    if intent == "list_users":
        return _handle_list_users(db, pq)
    if intent == "list_projects":
        return _handle_list_projects(db)
    if intent == "bug_detail":
        from app.chatbot.memory import store as _mem
        if pq.bug_id:
            _mem.remember_bug(actor.id, pq.bug_id)
        return _handle_bug_detail(db, pq)
    if intent == "stats":
        return _handle_stats(db)
    if intent == "recent_activity":
        return _handle_recent_activity(db, pq, actor)
    if intent == "list_bugs":
        return _handle_list_bugs(db, pq, ctx)
    return None


def _classifier_action_invalid(pred_intent: str) -> Response:
    """Friendly "tell me more" response when the classifier guesses an
    action intent but the rule parser couldn't fill in the slots."""
    verb = pred_intent[len("action_"):]
    return Response(
        blocks=[Block("text", {"text":
            f"I think you want to **{verb}** "
            f"something, but I couldn't pin down which bug or who. "
            f"Try a more concrete phrasing — for example: "
            f"*assign bug 5 to alice* or *close #12*."})],
        summary=f"Classifier guessed {pred_intent}",
        intent="action_invalid",
    )


def _try_classifier(message: str, db: Session, pq, actor: User, ctx) -> Optional[Response]:
    """Layer 2 fallback: ask the statistical classifier whether the message
    looks like a known read intent. Returns the handler's Response or None."""
    from app.chatbot import classifier as _clf
    pred = _clf.predict(message)
    if pred is None:
        return None
    read_resp = _dispatch_read_intent(pred.intent, db, pq, actor, ctx)
    if read_resp is not None:
        return read_resp
    if pred.intent.startswith("action_"):
        return _classifier_action_invalid(pred.intent)
    return None


def _try_llm(message: str, db: Session, actor: User) -> Optional[Response]:
    """Layer 3 fallback: optional local LLM. Returns None if unavailable
    or if it failed — an LLM fault must NEVER take down the chat path."""
    try:
        from app.chatbot import llm as _llm
        if _llm.is_available():
            return _llm.try_understand(message, db, actor)
    except Exception:
        import logging
        logging.getLogger("bug_hunter.sleuth").exception(
            "Sleuth LLM fallback raised — swallowing and returning unknown."
        )
    return None


def execute(message: str, db: Session, actor: User,
            now: Optional[datetime] = None) -> Response:
    """Parse the message and dispatch to the right handler.

    `now` lets tests inject a fixed timestamp; defaults to UTC now.
    """
    now = now or datetime.now(timezone.utc)
    ctx = build_context(db)
    pq = parse(message, ctx, now=now)

    _resolve_pronouns(pq, actor)
    _resolve_me_pronoun(pq, actor)

    # Confirmation answers come BEFORE everything else.
    if pq.intent == "confirm_yes":
        return _handle_confirm_yes(db, actor)
    if pq.intent == "confirm_no":
        return _handle_confirm_no(actor)

    if pq.intent.startswith("action_"):
        return _handle_action_request(pq, actor)

    read_resp = _dispatch_read_intent(pq.intent, db, pq, actor, ctx)
    if read_resp is not None:
        return read_resp

    classifier_resp = _try_classifier(message, db, pq, actor, ctx)
    if classifier_resp is not None:
        return classifier_resp

    llm_resp = _try_llm(message, db, actor)
    if llm_resp is not None:
        return llm_resp

    return _handle_unknown(message)


__all__ = ["Block", "Response", "execute", "build_context"]
