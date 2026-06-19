"""Sleuth API router.

Two endpoints:

  POST /api/chat/ask                — accepts {"message": "..."} and returns
                                      the structured Sleuth response.
  GET  /api/chat/download/{token}   — streams a staged Excel workbook.

Both require an authenticated user (same cookie as the rest of the SPA),
so the chatbot honors session revocation and forced-logout exactly like
the rest of the app — a revoked admin can't keep using the chatbot.

Why a separate router (instead of folding into routes/bugs.py)?

  - It's a different surface — natural-language vs structured CRUD —
    and shoving NLU inside the bugs router would muddle both.
  - The chat code is read-only and deserves a clear boundary so a
    future contributor can't accidentally introduce a write path.
  - The download endpoint has its own caching / streaming behavior
    that doesn't belong next to the bug attachment downloads.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from datetime import timedelta

from app.auth import get_current_user, require_admin
from app.config import get_settings
from app.database import get_db
from app.models import ChatConversation, ChatMessage, User, _utcnow

from . import excel, executor

logger = logging.getLogger("bug_hunter.chatbot")

# FastAPI dependency aliases. The Annotated form is the documented idiom —
# it keeps the injection out of the parameter default.
DbDep = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
# The document-ingest endpoint is the ONE admin-only write surface in Sleuth.
AdminUser = Annotated[User, Depends(require_admin)]


def _persist_turn(db: Session, actor: User, user_msg: str,
                  resp: "executor.Response") -> None:
    """Append the user message + assistant reply to the durable chat_*
    transcript. Additive, gated, and best-effort — any failure is swallowed
    so persistence problems never affect the chat response.

    One rolling conversation per user: we reuse the most recent conversation
    if it was touched in the last 30 minutes, otherwise start a fresh one.
    """
    if not get_settings().SLEUTH_CHAT_MEMORY_ENABLED:
        return
    try:
        cutoff = _utcnow() - timedelta(minutes=30)
        conv = (
            db.query(ChatConversation)
            .filter(ChatConversation.user_id == actor.id,
                    ChatConversation.updated_at >= cutoff)
            .order_by(ChatConversation.updated_at.desc())
            .first()
        )
        if conv is None:
            conv = ChatConversation(user_id=actor.id)
            db.add(conv)
            db.flush()
        # Engine label from the intent prefix for lightweight observability.
        engine = "rules"
        if resp.intent.startswith("cloud_"):
            engine = "cloud"
        elif resp.intent in {"unknown", "error"}:
            engine = ""
        # Store what the assistant actually SAID (first text block) so the
        # cloud layer's rolling history is meaningful; fall back to the
        # one-line summary for table/file-only replies.
        said = next(
            (b.payload.get("text", "") for b in resp.blocks if b.kind == "text"),
            "",
        ) or (resp.summary or "")
        db.add(ChatMessage(conversation_id=conv.id, role="user",
                           content=user_msg[:4000], engine=""))
        db.add(ChatMessage(conversation_id=conv.id, role="assistant",
                           content=said[:4000], engine=engine))
        conv.updated_at = _utcnow()
        db.commit()
    except Exception:  # noqa: BLE001 — transcript is non-critical
        db.rollback()
        logger.debug("Sleuth chat transcript persist failed", exc_info=True)

router = APIRouter(prefix="/api/chat", tags=["chatbot"])


# ---------------------------------------------------------------------------
# Per-user soft rate limit. We only need the lightest of guards here — the
# rule engine is cheap, but Excel exports do real CPU work and we don't
# want a malformed loop on the client to hammer the box.
# ---------------------------------------------------------------------------
_RATE_WINDOW_SECONDS = 60
_RATE_MAX_REQUESTS = 30   # 30 chat asks / minute / user
_rate_state: dict[int, list[float]] = {}
# Sync endpoints run in a threadpool, so concurrent same-user asks race the
# read-modify-write on a bucket; guard it like the other shared caches.
_rate_lock = threading.Lock()


def _check_rate(user_id: int) -> None:
    now = time.time()
    cutoff = now - _RATE_WINDOW_SECONDS
    with _rate_lock:
        bucket = _rate_state.setdefault(user_id, [])
        # Drop timestamps older than the window (O(window), window is tiny).
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= _RATE_MAX_REQUESTS:
            raise HTTPException(
                status_code=429,
                detail="Too many chatbot requests, slow down a moment",
            )
        bucket.append(now)


# ---------------------------------------------------------------------------
# I/O models
# ---------------------------------------------------------------------------
class ChatIn(BaseModel):
    """Inbound chat message. Capped to a sensible length so a runaway
    paste doesn't get parsed (and so we never feed an unbounded string
    into the optional LLM passthrough either)."""
    message: str = Field(min_length=1, max_length=2000)


class _BlockOut(BaseModel):
    kind: str
    payload: dict


class ChatOut(BaseModel):
    blocks: list[_BlockOut]
    summary: str
    intent: str


# ---------------------------------------------------------------------------
# /api/chat/ask
# ---------------------------------------------------------------------------
@router.post(
    "/ask",
    responses={
        429: {"description": "Rate limit exceeded — too many chatbot requests."},
    },
)
def ask(
    payload: ChatIn,
    db: DbDep,
    actor: CurrentUser,
) -> ChatOut:
    """Answer a natural-language question.

    Always returns 200 unless something genuinely unexpected blew up — a
    "no results" or "I didn't understand" reply is part of the contract,
    not an error condition.
    """
    _check_rate(actor.id)

    try:
        resp = executor.execute(payload.message, db, actor)
    except HTTPException:
        # Auth / role exceptions from underlying calls — pass through.
        raise
    except Exception as exc:   # noqa: BLE001 — we deliberately never crash the chat
        # Log with a stack trace, but reply gracefully so the chat stays
        # usable. A panic here looks bad to a user typing innocent input.
        logger.exception("Sleuth executor failed: %s", exc)
        return ChatOut(
            blocks=[_BlockOut(kind="text", payload={
                "text": "Sorry — something went wrong on my side while "
                        "answering that. The error was logged. Please "
                        "try rephrasing",
            })],
            summary="Internal error",
            intent="error",
        )

    # Durable transcript (additive chat_* tables). Best-effort.
    _persist_turn(db, actor, payload.message, resp)

    return ChatOut(
        blocks=[_BlockOut(kind=b.kind, payload=b.payload) for b in resp.blocks],
        summary=resp.summary,
        intent=resp.intent,
    )


# ---------------------------------------------------------------------------
# /api/chat/ingest  — admin uploads a document; Sleuth turns it into bugs.
#
# This is Sleuth's single deliberate WRITE surface and it is ADMIN-ONLY (the
# AdminUser dependency 403s everyone else). The heavy lifting — AI / parser
# extraction and item creation — lives in app/chatbot/ingest.py; this endpoint
# is just upload plumbing + a chat-shaped reply the panel renders inline.
# ---------------------------------------------------------------------------
_INGEST_CHUNK = 256 * 1024


async def _read_upload_limited(file: UploadFile, limit: int) -> bytes:
    """Stream the upload, aborting with 413 before it can exceed `limit`."""
    buf = bytearray()
    while True:
        chunk = await file.read(_INGEST_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"Document too large. Max {limit // (1024 * 1024)} MB.",
            )
    return bytes(buf)


def _ingest_text_reply(text: str, intent: str) -> ChatOut:
    return ChatOut(
        blocks=[_BlockOut(kind="text", payload={"text": text})],
        summary=text[:80], intent=intent,
    )


def _ingest_preview_reply(specs: list, method: str, filename: str, project_name: str) -> ChatOut:
    """Conversational preview — Sleuth shows what it READ and asks before
    creating anything. The actual creation happens later, when the admin replies
    'create them' (handled by the executor)."""
    n = len(specs)
    how = "read it with AI" if method == "ai" else "parsed it"
    head = (
        f"📄 I {how} and found **{n}** work item{'' if n == 1 else 's'} in "
        f"**{filename}**. If you want, I'll add them to project "
        f"**{project_name}** — just reply **create them** (or **cancel**)."
    )
    rows = [[s["title"], s.get("priority", "Medium"), s.get("item_type", "Bug")]
            for s in specs[:50]]
    blocks = [
        _BlockOut(kind="text", payload={"text": head}),
        _BlockOut(kind="table", payload={"headers": ["Title", "Priority", "Type"], "rows": rows}),
    ]
    if n > len(rows):
        blocks.append(_BlockOut(kind="text", payload={"text": f"…and {n - len(rows)} more."}))
    blocks.append(_BlockOut(kind="suggestions", payload={"items": [
        {"label": f"✓ Create all {n}", "send": "create them"},
        {"label": "Cancel", "send": "cancel"},
    ]}))
    return ChatOut(blocks=blocks, summary=f"Found {n} items — awaiting confirmation",
                   intent="ingest_preview")


@router.post(
    "/ingest",
    responses={
        400: {"description": "No project to file the imported items under."},
        403: {"description": "Document ingest is admin-only."},
        413: {"description": "Uploaded document is too large."},
        429: {"description": "Rate limit exceeded — too many requests."},
    },
)
async def ingest_document(
    db: DbDep,
    actor: AdminUser,
    file: Annotated[UploadFile, File()],
    project_id: Annotated[Optional[int], Form()] = None,
) -> ChatOut:
    """Admin-only: READ an uploaded document (xlsx / CSV / JSON / text) and tell
    the admin what work items it contains. Nothing is created here — Sleuth
    stages the candidates and only creates them when the admin replies
    'create them' in the chat (see the executor's ingest-create handler)."""
    _check_rate(actor.id)
    from app.chatbot import ingest as _ingest
    from app.chatbot.memory import store as _mem

    data = await _read_upload_limited(file, _ingest.MAX_DOC_BYTES)
    if not data:
        return _ingest_text_reply("That file was empty — there was nothing to read.", "ingest_empty")

    specs, method = _ingest.extract_specs(file.filename or "", data)
    if not specs:
        return _ingest_text_reply(
            "I read that document but couldn't find any work items in it. I work "
            "best with a list (one item per line), a CSV / Excel sheet with a "
            "**Title** column, or a JSON array of `{title, priority, ...}` objects.",
            "ingest_empty",
        )
    project = _ingest.resolve_project_for_preview(db, project_id)
    if project is None:
        raise HTTPException(
            status_code=400,
            detail="There's no project to file these into yet — create one first.",
        )
    _mem.stage_ingest(actor.id, {
        "specs": specs, "filename": file.filename or "document",
        "project_id": project.id, "project_name": project.name, "method": method,
    })
    return _ingest_preview_reply(specs, method, file.filename or "document", project.name)


# ---------------------------------------------------------------------------
# /api/chat/download/{token}
# ---------------------------------------------------------------------------
@router.get(
    "/download/{token}",
    responses={
        404: {"description": "Download link has expired or is no longer valid."},
    },
)
def download_staged(
    token: str,
    _user: CurrentUser,
):
    """Stream a previously-staged Excel workbook.

    The token is opaque and cryptographically random (24 url-safe bytes)
    so guessing it is computationally infeasible. We still require an
    authenticated session — the token alone is not a capability."""
    entry = excel.fetch_staged(token)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="That download link has expired or is no longer valid",
        )
    payload, filename = entry

    # Force a download with the suggested filename. We intentionally do
    # NOT inline xlsx in the browser even though Chromium can preview it
    # — keeping it as an attachment matches user expectation when they
    # asked for a file.
    safe_filename = filename.replace('"', "_").replace("\r", "").replace("\n", "")
    return StreamingResponse(
        iter([payload]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "Content-Length": str(len(payload)),
            # Private, never cached — the link is short-lived anyway.
            "Cache-Control": "private, no-store, max-age=0",
        },
    )


# Re-export for `from app.chatbot import router` style imports if anyone
# elsewhere wants them.
__all__ = ["router"]
