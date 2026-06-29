"""Per-user conversation memory for the Sleuth assistant.

Conversations are stateful: people say "close it" after viewing a bug, or
"assign her to bug 5" after listing managers. This module tracks recent
referents in a small, in-process, TTL-cleaned store.

- Storage is a plain dict keyed by user_id; it does not survive process
  restarts, so context always starts fresh.
- A single lock makes all read-modify-write operations thread-safe.
- A cap of 200 sessions and a 30-minute TTL keep RAM bounded.

Fields tracked per session:
- last_bug_id     resolves pronouns like "it", "that bug"
- last_user_id    resolves "her"/"him" after a user is mentioned
- last_filter     most recent ParsedQuery filter dict (enables refinements)
- pending_action  serialized ActionPlan awaiting "yes"/"confirm"
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# 200 sessions at ~1 KB each keeps total memory under ~200 KB.
_MAX_SESSIONS = 200
_TTL_SECONDS = 30 * 60   # 30 minutes idle
# Shorter confirm window than the session TTL: a stray "ok" long after a
# forgotten proposal must not fire a destructive write.
_CONFIRM_TTL_SECONDS = 5 * 60   # 5 minutes


@dataclass
class _Session:
    """The mutable state we keep for one user."""
    last_bug_id: Optional[int] = None
    last_user_id: Optional[int] = None
    last_user_name: Optional[str] = None
    last_filter: dict[str, Any] = field(default_factory=dict)
    pending_action: Optional[dict[str, Any]] = None
    # Separate from last_seen so later activity doesn't extend the confirm window.
    pending_staged_at: float = 0.0
    # Candidate bug specs parsed from an uploaded document; only created on
    # explicit confirmation, never on upload alone.
    pending_ingest: Optional[dict[str, Any]] = None
    pending_ingest_staged_at: float = 0.0
    last_seen: float = 0.0   # epoch seconds, for TTL eviction


class _Store:
    """Thread-safe in-process session store.

    All mutating operations hold the lock and refresh last_seen, so ongoing
    activity keeps a user's context alive.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, _Session] = {}

    # -- internal --------------------------------------------------------
    def _evict_expired_locked(self, now: float) -> None:
        # Caller must hold the lock.
        dead = [uid for uid, s in self._sessions.items()
                if (now - s.last_seen) > _TTL_SECONDS]
        for uid in dead:
            self._sessions.pop(uid, None)

    def _evict_oldest_locked(self) -> None:
        # Drop the LRU session when at capacity.
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
        """Read a session without extending its TTL or creating one."""
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
            # Defensive copy — the caller may mutate its own dict afterward.
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

        Returns None if there is no pending action, the session has expired,
        or the confirm window has closed. Clears a stale proposal rather than
        executing on a late "yes".
        """
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            s = self._sessions.get(user_id)
            if s is None or s.pending_action is None:
                return None
            if (now - s.pending_staged_at) > _CONFIRM_TTL_SECONDS:
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
        """Return the staged ingest if it's within the confirm window, else clear and return None."""
        if s is None or s.pending_ingest is None:
            return None
        if (now - s.pending_ingest_staged_at) > _CONFIRM_TTL_SECONDS:
            s.pending_ingest = None
            return None
        return s.pending_ingest

    def peek_ingest(self, user_id: int) -> Optional[dict[str, Any]]:
        """Return the staged ingest without consuming it (None if none/expired)."""
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
        """Wipe a user's session entirely (e.g. when they clear the chat)."""
        with self._lock:
            self._sessions.pop(user_id, None)

    # -- test helpers ----------------------------------------------------
    def _all_sessions_for_test(self) -> dict[int, _Session]:
        with self._lock:
            return dict(self._sessions)

    def _clear_all_for_test(self) -> None:
        with self._lock:
            self._sessions.clear()


# Module-level singleton.
store = _Store()


__all__ = ["store"]
