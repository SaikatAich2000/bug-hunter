"""Per-account login lockout.

In-memory sliding-window counter keyed by email. After N failed attempts in a
rolling window, the email is locked for L seconds and subsequent requests are
rejected with 429 before the bcrypt verify runs.

Notes:
  - Keyed by email, not by IP. The IP rate limit in app/main.py complements
    this, so an attacker proxying through many IPs is still stopped by the
    per-account counter.
  - Unknown emails also tick the counter. Ticking only known emails would let
    an attacker enumerate accounts by which addresses do or do not lock.
  - Lockout is a DoS vector: a hostile party can lock a target user's account
    by spamming bad logins. Operators who can't accept that trade-off should
    keep the limit high and the window short, or disable via LOGIN_FAIL_LIMIT=0.
  - Multi-worker uvicorn deployments get per-worker buckets, so the effective
    limit is N * threshold. For stricter global enforcement, push limits into
    nginx (limit_req) or a shared store.
  - Memory is bounded by _LOCKOUT_BUCKETS_MAX.
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock

from fastapi import HTTPException


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except ValueError:
        return default


# All three limits are overridable via environment variables.
_LOGIN_FAIL_LIMIT = _env_int("LOGIN_FAIL_LIMIT", 10)
_LOGIN_FAIL_WINDOW_SECONDS = _env_int("LOGIN_FAIL_WINDOW_SECONDS", 900)  # 15 min
_LOGIN_LOCKOUT_SECONDS = _env_int("LOGIN_LOCKOUT_SECONDS", 900)          # 15 min
_LOCKOUT_BUCKETS_MAX = 10_000


@dataclass
class _Bucket:
    fails: deque = field(default_factory=deque)
    locked_until: float = 0.0


_buckets: dict[str, _Bucket] = {}
_lock = Lock()


def _key(email: str) -> str:
    return (email or "").strip().lower()


def _evict_old(bucket: _Bucket, cutoff: float) -> None:
    while bucket.fails and bucket.fails[0] < cutoff:
        bucket.fails.popleft()


def _reclaim_buckets(now: float) -> None:
    """Bound _buckets without dropping an active lock.

    A plain FIFO eviction could drop a currently-locked bucket, letting an
    attacker churn >10k unique emails to flush a victim's lock and bypass the
    lockout. Instead, reclaim only idle buckets (not locked and no in-window
    failures); if still at the cap, evict the least-recently-active unlocked
    bucket. Only when every bucket is locked do we drop the soonest-to-expire
    lock as a memory backstop. Caller holds _lock.
    """
    cutoff = now - _LOGIN_FAIL_WINDOW_SECONDS
    dead = [
        k for k, b in _buckets.items()
        if b.locked_until <= now and (not b.fails or b.fails[-1] < cutoff)
    ]
    for k in dead:
        del _buckets[k]
    if len(_buckets) >= _LOCKOUT_BUCKETS_MAX and _buckets:
        unlocked = [k for k, b in _buckets.items() if b.locked_until <= now]
        if unlocked:
            oldest = min(
                unlocked,
                key=lambda k: _buckets[k].fails[-1] if _buckets[k].fails else 0.0,
            )
        else:
            oldest = min(_buckets, key=lambda k: _buckets[k].locked_until)
        del _buckets[oldest]


def check_locked(email: str) -> None:
    """Raise 429 if the account is currently locked out. Called before the
    password verify so the bcrypt cost isn't paid during a lockout flood."""
    if _LOGIN_FAIL_LIMIT <= 0:
        return
    now = time.monotonic()
    key = _key(email)
    with _lock:
        bucket = _buckets.get(key)
        if bucket is None or bucket.locked_until <= now:
            return
        retry_after = max(1, int(bucket.locked_until - now))
    raise HTTPException(
        status_code=429,
        detail="Too many failed sign-in attempts. Please try again later.",
        headers={"Retry-After": str(retry_after)},
    )


def record_failure(email: str) -> None:
    """Increment the failure counter for this email. If the rolling
    window has accumulated >= LOGIN_FAIL_LIMIT failures, set the lockout
    timestamp. Called only when the login itself failed."""
    if _LOGIN_FAIL_LIMIT <= 0:
        return
    now = time.monotonic()
    cutoff = now - _LOGIN_FAIL_WINDOW_SECONDS
    key = _key(email)
    with _lock:
        bucket = _buckets.get(key)
        if bucket is None:
            if len(_buckets) >= _LOCKOUT_BUCKETS_MAX:
                # Bound memory without dropping an active lock.
                _reclaim_buckets(now)
            bucket = _Bucket()
            _buckets[key] = bucket
        _evict_old(bucket, cutoff)
        bucket.fails.append(now)
        if len(bucket.fails) >= _LOGIN_FAIL_LIMIT:
            bucket.locked_until = now + _LOGIN_LOCKOUT_SECONDS
            # Clear stale timestamps so the account isn't immediately
            # re-locked when the lockout expires (relevant when
            # LOGIN_FAIL_WINDOW_SECONDS > LOGIN_LOCKOUT_SECONDS).
            bucket.fails.clear()


def clear(email: str) -> None:
    """Reset the bucket for this email. Called on successful login so a
    user doesn't carry forward earlier failures."""
    key = _key(email)
    with _lock:
        _buckets.pop(key, None)


def _reset_for_tests() -> None:
    """Wipe all state. Tests call this between cases so buckets don't
    leak across the suite."""
    with _lock:
        _buckets.clear()
