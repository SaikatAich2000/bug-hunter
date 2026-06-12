"""Sleuth Layer 4 — optional cloud LLM (Gemini primary, OpenRouter fallback).

This is the natural-language fallback for the ~5% of questions the rules
(Layer 1), the classifier (Layer 2) and the optional local model (Layer 3)
can't handle. It is the ONLY part of Sleuth that calls an external API, and
it is OFF unless the operator sets SLEUTH_CLOUD_ENABLED and pastes a key.

Two hard safety rules, enforced in code below — not just in the prompt:

  1. NEVER invents data. A question that needs facts ("how many bugs did
     John close last week?") is turned into a *canonical query phrase* and
     re-parsed by the deterministic NLU, so the numbers come from real SQL
     SELECTs. The model picks the filter; the database produces the answer.

  2. NEVER writes. If the model's canonical query parses to an `action_*`
     (close/delete/assign/comment/...) intent we DROP it and fall through.
     Writes happen only when the *user themselves* types the command and
     confirms it via the existing rule-based Yes/Cancel flow.

Everything sent outbound is run through redaction.redact() first. Any
failure (no key, timeout, bad JSON, both providers down) returns None and
the chat falls back to the normal "I didn't understand" reply — a cloud
fault can never take the chat path down.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User
from app.chatbot.executor import Block, Response
from app.chatbot.redaction import redact

logger = logging.getLogger("bug_hunter.sleuth.cloud")


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------
def is_available() -> bool:
    """True iff the operator enabled the layer AND gave us at least one key
    AND httpx is importable. Cheap to call on every request."""
    s = get_settings()
    if not s.SLEUTH_CLOUD_ENABLED:
        return False
    if not (s.GEMINI_API_KEY or s.OPENROUTER_API_KEY):
        return False
    try:
        import httpx  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
# The model returns a single JSON object. It either routes a data question
# back to our SQL handlers (mode="data") or answers a free-form question
# from the supplied CONTEXT (mode="answer"). It must NEVER produce counts
# or write actions itself.
SYSTEM_PROMPT = (
    "You are Sleuth, the sharp, friendly AI assistant built into the Bug "
    "Hunter issue tracker. Be a smart teammate: natural, varied, concise "
    "(1-4 sentences), never robotic. Use the recent conversation and any "
    "CONTEXT block. NEVER repeat your previous reply word-for-word — if the "
    "user pushes back or repeats themselves, acknowledge it and change tack.\n"
    "\n"
    "ABOUT THE APP (use this to answer questions accurately):\n"
    "- Work items live in projects. Each item is a Bug, Requirement or Task "
    "with status (New / In Progress / Resolved / Closed / Reopened), priority "
    "(Low / Medium / High / Critical), environment (DEV / UAT / PROD), an "
    "optional due date, assignees, comments and attachments. Events group "
    "items for standups/sprints.\n"
    "- Roles: admin (manages users & sessions, full access), manager (can "
    "edit any item or project), user (can only edit items they reported or "
    "are assigned to). Only ADMINS can manage accounts or revoke login "
    "sessions — from the web app's admin/Sessions panel, not through chat. "
    "If someone is not an admin, they cannot revoke sessions; suggest asking "
    "an admin.\n"
    "- YOU can: look up and summarise tracker data (lists, counts, stats, "
    "recent activity, per-person reports, bug details, Excel exports) and "
    "explain how to use the app.\n"
    "- YOU cannot: change data or settings yourself. Creating, closing, "
    "assigning, commenting, deleting etc. happen when the USER types the "
    "command (e.g. \"create bug Login crash\", \"close #12\", \"assign bug 5 "
    "to alice\", \"comment on #3: fixed\") and confirms the Yes/Cancel "
    "prompt. Never claim you performed or will perform a change.\n"
    "\n"
    "Reply with ONE JSON object and nothing else:\n"
    '  {"mode": "data"|"answer", "canonical_query": string, "text": string}\n'
    "\n"
    "• mode=\"data\" — whenever the user wants FACTS from the tracker (a "
    "list, count, stats, who-did-what, a bug's details, an export). You "
    "never answer these yourself: put a short canonical_query built ONLY "
    "from this vocabulary (the app runs it as a real, safe database query):\n"
    "    'open bugs' | 'closed bugs' | 'bugs with status <New|In Progress|"
    "Resolved|Closed|Reopened>' | 'bugs with priority <Low|Medium|High|"
    "Critical>' | 'bugs in environment <DEV|UAT|PROD>' | 'bugs in project "
    "<name>' | 'bugs assigned to <person>' | 'bugs reported by <person>' | "
    "'how many <any of the above>' (for counts) | 'export <any of the "
    "above> to excel' (for files) | 'stats' | 'recent activity' | 'activity "
    "by <person> last week' | 'report of who solved how many bugs last "
    "week' | 'list users' | 'list admins' | 'list projects' | 'bug <id>'\n"
    "  Filters can combine (e.g. 'how many critical bugs in PROD'). NEVER "
    "put a number or fact you computed into the query — you pick the "
    "FILTER, the database computes the answer. Leave text empty.\n"
    "\n"
    "• mode=\"answer\" — EVERYTHING else: small talk, capability questions "
    "(\"can you add bugs?\"), how-to, permissions questions (\"can I revoke "
    "a session if I'm not admin?\" → no, admins only), hypotheticals, "
    "complaints, and anything answerable from CONTEXT. Friendly text reply; "
    "no invented counts/names — if they actually want data, use mode="
    "\"data\" instead. Leave canonical_query empty.\n"
    "\n"
    "Prefer mode=\"answer\" over refusing; if a message is unclear, ask a "
    "short clarifying question or suggest something useful you CAN do."
)


# ---------------------------------------------------------------------------
# Provider calls (httpx REST — no SDK dependency)
# ---------------------------------------------------------------------------
def _call_gemini(system: str, user: str) -> Optional[str]:
    s = get_settings()
    if not s.GEMINI_API_KEY:
        return None
    import httpx
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{s.GEMINI_MODEL}:generateContent"
    )
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": s.SLEUTH_CLOUD_MAX_TOKENS,
            "responseMimeType": "application/json",
            # gemini-2.5-* "think" by default, which can silently eat the
            # whole output-token budget and return empty text. We only need
            # fast structured replies, so switch thinking off. Harmless on
            # models that don't support it (the field is ignored).
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        r = httpx.post(url, params={"key": s.GEMINI_API_KEY}, json=body,
                       timeout=s.SLEUTH_CLOUD_TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sleuth Gemini call failed: %s", exc)
        return None


def _call_openrouter(system: str, user: str) -> Optional[str]:
    s = get_settings()
    if not s.OPENROUTER_API_KEY:
        return None
    import httpx
    try:
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {s.OPENROUTER_API_KEY}"},
            json={
                "model": s.OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "max_tokens": s.SLEUTH_CLOUD_MAX_TOKENS,
                "response_format": {"type": "json_object"},
            },
            timeout=s.SLEUTH_CLOUD_TIMEOUT_S,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sleuth OpenRouter call failed: %s", exc)
        return None


# Circuit breaker: after BOTH providers fail (quota exhausted, outage), skip
# the cloud for a short window instead of paying a doomed network round-trip
# on every message. The rule-based layers keep answering meanwhile.
_COOLDOWN_S = 45.0
_cooldown_until = 0.0


def _complete(system: str, user_raw: str) -> Optional[dict[str, Any]]:
    """Redact, call Gemini, fall back to OpenRouter, parse the JSON reply.
    Returns the parsed dict or None on any failure."""
    global _cooldown_until
    user = redact(user_raw)
    raw = _call_gemini(system, user)
    if raw is None:
        raw = _call_openrouter(system, user)
    if not raw:
        _cooldown_until = time.time() + _COOLDOWN_S
        logger.info("Sleuth cloud: both providers failed; cooling down %.0fs",
                    _COOLDOWN_S)
        return None
    return _extract_json(raw)


def _extract_json(raw: str) -> Optional[dict[str, Any]]:
    """Pull the first balanced {...} out of the reply (tolerates fences)."""
    s = raw.strip().replace("```json", "").replace("```JSON", "").replace("```", "")
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except ValueError:
                    return None
    return None


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------
def try_understand(
    message: str,
    db: Session,
    actor: User,
    now: Optional[datetime] = None,
    history: str = "",
) -> Optional[Response]:
    """Layer 4 entry point. Returns a Response, or None to fall through.

    Data questions are answered by the deterministic SQL handlers (the
    model only chooses the filter); free-form questions are answered from
    retrieved context. Writes are never initiated here.
    """
    if not is_available():
        return None
    if time.time() < _cooldown_until:
        return None   # recent total provider failure — let the rules answer
    now = now or datetime.now(timezone.utc)

    # Pull a few recent turns so follow-ups ("and the critical ones?") have
    # context. Best-effort; redacted later with the rest of the prompt.
    if not history:
        history = _recent_history(db, actor)

    # Retrieve grounding context (bugs / comments / docs). Best-effort —
    # retrieval problems must not break the call.
    context_text = ""
    try:
        from app.chatbot import rag
        context_text = rag.retrieve_text(message, db, actor)
    except Exception:  # noqa: BLE001
        logger.debug("Sleuth RAG retrieval skipped/failed", exc_info=True)

    prompt = message
    if history:
        prompt = f"Recent conversation:\n{history}\n\nUser question: {message}"
    if context_text:
        prompt = f"{prompt}\n\nCONTEXT:\n{context_text}"

    parsed = _complete(SYSTEM_PROMPT, prompt)
    if not parsed:
        return None

    mode = str(parsed.get("mode") or "").strip().lower()

    if mode == "data":
        return _route_data_query(str(parsed.get("canonical_query") or ""),
                                 db, actor, now)
    if mode == "answer":
        text = str(parsed.get("text") or "").strip()
        if not text:
            return None
        return Response(
            blocks=[Block(kind="text", payload={"text": text})],
            summary="Answered from your data",
            intent="cloud_answer",
        )
    return None


def _recent_history(db: Session, actor: User, limit: int = 6) -> str:
    """Return the last few transcript turns for this user as
    'role: text' lines, oldest first. Best-effort; "" on any problem."""
    try:
        from app.models import ChatConversation, ChatMessage
        conv = (
            db.query(ChatConversation)
            .filter(ChatConversation.user_id == actor.id)
            .order_by(ChatConversation.updated_at.desc())
            .first()
        )
        if conv is None:
            return ""
        msgs = (
            db.query(ChatMessage)
            .filter(ChatMessage.conversation_id == conv.id)
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
            .all()
        )
        return "\n".join(f"{m.role}: {m.content}" for m in reversed(msgs))
    except Exception:  # noqa: BLE001
        return ""


def _route_data_query(canonical: str, db: Session, actor: User,
                      now: datetime) -> Optional[Response]:
    """Re-parse the model's canonical query with the deterministic NLU and
    dispatch it through the READ handlers only. This is what guarantees the
    numbers are real and that the cloud layer can never trigger a write."""
    canonical = canonical.strip()
    if not canonical:
        return None
    from app.chatbot.executor import build_context, _dispatch_read_intent
    from app.chatbot.nlu import parse

    ctx = build_context(db)
    pq = parse(canonical, ctx, now=now)

    # Hard stop: the cloud layer must never reach a write path, even if the
    # model emitted (or the parser inferred) an action verb.
    if pq.intent.startswith("action_") or pq.intent in {"confirm_yes", "confirm_no"}:
        logger.info("Sleuth cloud: dropping non-read canonical query %r", canonical)
        return None

    resp = _dispatch_read_intent(pq.intent, db, pq, actor, ctx)
    if resp is not None:
        resp.intent = f"cloud_data:{pq.intent}"
    return resp


__all__ = ["is_available", "try_understand", "SYSTEM_PROMPT"]
