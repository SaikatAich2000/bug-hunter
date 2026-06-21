"""Per-user conversation memory for the Sleuth assistant.

Conversations are stateful: people say "close it" after viewing a bug, or
"and assign her to bug 5" after listing managers. This module tracks recent
referents in a small, in-process, TTL-cleaned store.

Design notes:
- Storage is a plain dict keyed by user_id. It does not persist across
  process restarts: after a restart the conversation starts fresh, with no
  stale referents.
- Access is thread-safe via a single lock; read-modify-writes are short.
- Hard caps on total sessions (200) and per-session entry size keep RAM
  bounded.
- TTL is 30 minutes; after that a pronoun like "it" no longer resolves.

The state stored is minimal:
- last_bug_id          — for pronouns like "it", "that bug"
- last_user_id         — for "her", "him" after listing/mentioning a user
- last_filter          — the most recent ParsedQuery filter dict
                         (so "and only the criticals" can refine it)
- pending_action       — a serialized ActionPlan awaiting "yes"/"confirm"
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# 200 sessions at under 1 KB each keeps total memory under ~200 KB.
_MAX_SESSIONS = 200
_TTL_SECONDS = 30 * 60   # 30 minutes idle
# A staged write stays confirmable only briefly. The session TTL above is about
# idle context; this is specifically so a stray "ok"/"sure" long after a
# forgotten proposal can't fire a destructive write.
_CONFIRM_TTL_SECONDS = 5 * 60   # 5 minutes


@dataclass
class _Session:
    """The mutable state we keep for one user."""
    last_bug_id: Optional[int] = None
    last_user_id: Optional[int] = None
    last_user_name: Optional[str] = None
    last_filter: dict[str, Any] = field(default_factory=dict)
    pending_action: Optional[dict[str, Any]] = None
    # Epoch seconds when pending_action was staged — drives the confirm window
    # (kept separate from last_seen, which any later activity refreshes).
    pending_staged_at: float = 0.0
    # A document an admin uploaded to Sleuth, parsed into candidate bug specs
    # and parked here awaiting an explicit "create them". Creation happens only
    # on that confirmation, never on upload alone.
    pending_ingest: Optional[dict[str, Any]] = None
    # Epoch seconds when pending_ingest was staged — same short confirm window as
    # pending_action, so a stray late "ok" can't fire a forgotten bulk create.
    pending_ingest_staged_at: float = 0.0
    last_seen: float = 0.0   # epoch seconds, for TTL eviction


class _Store:
    """Thread-safe session store.

    All mutating operations take the lock and update last_seen on the
    session, so ongoing activity keeps a user's context alive.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, _Session] = {}

    # -- internal --------------------------------------------------------
    def _evict_expired_locked(self, now: float) -> None:
        # Prune anything older than the TTL. Caller must hold the lock.
        dead = [uid for uid, s in self._sessions.items()
                if (now - s.last_seen) > _TTL_SECONDS]
        for uid in dead:
            self._sessions.pop(uid, None)

    def _evict_oldest_locked(self) -> None:
        # Cap total sessions: drop the least-recently-used one.
        if len(self._sessions) < _MAX_SESSIONS:
            return
        oldest_uid = min(self._sessions.items(),
                         key=lambda kv: kv[1].last_seen)[0]
        self._sessions.pop(oldest_uid, None)

    def _get_or_create_locked(self, user_id: int, now: float) -> _Session:
        s = self._sessions.get(user_id)
        if s is None:
            self._evict_expired_locked(now)
            self._evict_oldest_locked()
            s = _Session(last_seen=now)
            self._sessions[user_id] = s
        return s

    # -- public ----------------------------------------------------------
    def touch(self, user_id: int) -> _Session:
        """Return (or create) a session and update last_seen."""
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            s.last_seen = now
            return s

    def get(self, user_id: int) -> Optional[_Session]:
        """Read a session WITHOUT extending its TTL.

        Useful for code paths that want to consult memory without
        creating a session (e.g. introspection, debug).
        """
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            return self._sessions.get(user_id)

    def remember_bug(self, user_id: int, bug_id: int) -> None:
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            s.last_bug_id = bug_id
            s.last_seen = now

    def remember_user(self, user_id: int,
                      target_user_id: int,
                      target_user_name: str) -> None:
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            s.last_user_id = target_user_id
            s.last_user_name = target_user_name
            s.last_seen = now

    def remember_filter(self, user_id: int, filter_dict: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            # Defensive copy — the caller may keep mutating its own copy.
            s.last_filter = dict(filter_dict)
            s.last_seen = now

    def stage_pending(self, user_id: int, action: dict[str, Any]) -> None:
        """Park an action awaiting user confirmation."""
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            s.pending_action = dict(action)
            s.pending_staged_at = now
            s.last_seen = now

    def take_pending(self, user_id: int) -> Optional[dict[str, Any]]:
        """Pop and return the staged action (single-use).

        Returns None if there isn't one, the session has expired, or the short
        confirm window has lapsed (so a stale "yes" can't fire a forgotten write).
        """
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            s = self._sessions.get(user_id)
            if s is None or s.pending_action is None:
                return None
            if (now - s.pending_staged_at) > _CONFIRM_TTL_SECONDS:
                # Proposal went stale — drop it rather than execute on a late "ok".
                s.pending_action = None
                return None
            action = s.pending_action
            s.pending_action = None
            s.last_seen = now
            return action

    def clear_pending(self, user_id: int) -> None:
        with self._lock:
            s = self._sessions.get(user_id)
            if s is not None:
                s.pending_action = None
                s.last_seen = time.time()

    def stage_ingest(self, user_id: int, data: dict[str, Any]) -> None:
        """Park a parsed document's candidate specs awaiting 'create them'."""
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            s.pending_ingest = dict(data)
            s.pending_ingest_staged_at = now
            s.last_seen = now

    def _ingest_if_fresh(self, s: Optional["_Session"], now: float) -> Optional[dict[str, Any]]:
        """The staged ingest if present AND within the confirm window; clears a
        stale one so a late 'ok' can't fire a forgotten bulk create."""
        if s is None or s.pending_ingest is None:
            return None
        if (now - s.pending_ingest_staged_at) > _CONFIRM_TTL_SECONDS:
            s.pending_ingest = None
            return None
        return s.pending_ingest

    def peek_ingest(self, user_id: int) -> Optional[dict[str, Any]]:
        """Return the staged ingest WITHOUT consuming it (None if none/expired)."""
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            return self._ingest_if_fresh(self._sessions.get(user_id), now)

    def take_ingest(self, user_id: int) -> Optional[dict[str, Any]]:
        """Pop and return the staged ingest (single-use)."""
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            s = self._sessions.get(user_id)
            data = self._ingest_if_fresh(s, now)
            if data is None:
                return None
            s.pending_ingest = None
            s.last_seen = now
            return data

    def reset(self, user_id: int) -> None:
        """Wipe a user's entire session — used when they 'clear' the chat."""
        with self._lock:
            self._sessions.pop(user_id, None)

    # -- test hooks ------------------------------------------------------
    def _all_sessions_for_test(self) -> dict[int, _Session]:
        with self._lock:
            return dict(self._sessions)

    def _clear_all_for_test(self) -> None:
        with self._lock:
            self._sessions.clear()


# Module-level singleton. Importers use this directly.
store = _Store()


__all__ = ["store"]
