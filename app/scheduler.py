"""Optional in-process scheduler for the daily email digest.

Driven entirely by configuration: when ``EMAIL_DIGEST_CRON`` holds a standard
5-field cron expression, the app runs ``app.jobs.email_digest`` itself on that
schedule — no host cron / Task Scheduler / CronJob needed. An empty expression
(the default) leaves scheduling to an external runner, preserving the previous
behaviour exactly.

Zero external dependencies: a tiny standard-cron parser/matcher plus one
asyncio task that wakes at the top of every minute. Anything that goes wrong
(an invalid expression, an unknown timezone, a failed run) only disables or
skips the scheduler — it never breaks app startup or the request path.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

from app.config import get_settings

logger = logging.getLogger("bug_hunter.scheduler")


def _parse_cron_part(part: str, lo: int, hi: int) -> range:
    """Parse ONE comma element of a cron field into the range it covers.

    Handles ``*``, a single value, ``a-b`` ranges and a trailing ``/n`` step.
    """
    part = part.strip()
    if not part:
        raise ValueError("empty element in cron field")
    step = 1
    has_step = "/" in part
    if has_step:
        base, step_s = part.split("/", 1)
        step = int(step_s)
        if step <= 0:
            raise ValueError("cron step must be a positive integer")
    else:
        base = part
    if base == "*":
        start, end = lo, hi
    elif "-" in base:
        a, b = base.split("-", 1)
        start, end = int(a), int(b)
    else:
        start = int(base)
        # Standard cron: "N/step" means "from N to the field max, every step"
        # (e.g. minute "5/15" → 5,20,35,50). A bare "N" (no step) is just N.
        end = hi if has_step else start
    if start < lo or end > hi or start > end:
        raise ValueError(f"cron value '{base}' out of range [{lo}, {hi}]")
    return range(start, end + 1, step)


def _parse_field(spec: str, lo: int, hi: int) -> set[int]:
    """Parse one cron field into the set of matching integers in ``[lo, hi]``.

    Supports ``*``, a single value, ``a-b`` ranges, ``*/n`` and ``a-b/n``
    steps, and comma-separated lists of any of those.
    """
    out: set[int] = set()
    for part in spec.split(","):
        out.update(_parse_cron_part(part, lo, hi))
    return out


class CronSchedule:
    """A parsed standard 5-field cron expression with a ``matches(dt)`` test.

    Fields are ``minute hour day-of-month month day-of-week`` (day-of-week
    ``0``/``7`` = Sunday). Honors the classic cron rule that when BOTH
    day-of-month and day-of-week are restricted, a tick matches if EITHER does.
    """

    __slots__ = (
        "minute", "hour", "dom", "month", "dow",
        "_dom_restricted", "_dow_restricted",
    )

    def __init__(self, expr: str) -> None:
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(
                f"cron expression must have 5 fields, got {len(fields)}: {expr!r}"
            )
        self.minute = _parse_field(fields[0], 0, 59)
        self.hour = _parse_field(fields[1], 0, 23)
        self.dom = _parse_field(fields[2], 1, 31)
        self.month = _parse_field(fields[3], 1, 12)
        dow = _parse_field(fields[4], 0, 7)
        if 7 in dow:  # cron allows both 0 and 7 for Sunday
            dow.discard(7)
            dow.add(0)
        self.dow = dow
        self._dom_restricted = fields[2] != "*"
        self._dow_restricted = fields[4] != "*"

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minute:
            return False
        if dt.hour not in self.hour:
            return False
        if dt.month not in self.month:
            return False
        dom_ok = dt.day in self.dom
        # Python weekday(): Mon=0..Sun=6 → cron dow: Sun=0..Sat=6.
        dow_ok = ((dt.weekday() + 1) % 7) in self.dow
        if self._dom_restricted and self._dow_restricted:
            return dom_ok or dow_ok
        if self._dom_restricted:
            return dom_ok
        if self._dow_restricted:
            return dow_ok
        return True


def _resolve_tz(name: str) -> tzinfo:
    """IANA timezone for the cron, or UTC when unset/unknown."""
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — unknown tz must not break startup
        logger.warning("Unknown EMAIL_DIGEST_TIMEZONE=%r; using UTC.", name)
        return timezone.utc


def _run_digest_once() -> dict:
    """Open a session and run the digest. Blocking — call via a thread."""
    from app.database import SessionLocal
    from app.jobs.email_digest import run_digest

    db = SessionLocal()
    try:
        return run_digest(db)
    finally:
        db.close()


async def _tick(schedule: CronSchedule, tz: tzinfo, now: datetime | None = None) -> dict | None:
    """Run the digest once if ``now`` matches the schedule. Returns the run
    stats (or ``None`` if it didn't fire / a run failed). Never raises."""
    now = now or datetime.now(tz)
    if not schedule.matches(now):
        return None
    try:
        stats = await asyncio.to_thread(_run_digest_once)
        logger.info(
            "Scheduled digest ran: %s email(s), %s operation(s), %s user(s).",
            stats["emails_sent"], stats["operations"], stats["users"],
        )
        return stats
    except Exception:  # noqa: BLE001 — a failed run must not kill the loop
        logger.exception("Scheduled digest run failed; will retry next tick.")
        return None


async def _loop(schedule: CronSchedule, tz: tzinfo) -> None:
    last_fired_minute: datetime | None = None
    while True:
        now = datetime.now(tz)
        # Wake at the start of the next minute, then evaluate the schedule.
        await asyncio.sleep(max(1.0, 60 - now.second - now.microsecond / 1_000_000))
        now = datetime.now(tz)
        minute = now.replace(second=0, microsecond=0)
        # Fire at most once per clock-minute: wake-time drift can land us back
        # in the same minute, which would otherwise double-run the digest.
        if minute == last_fired_minute:
            continue
        if schedule.matches(now):
            last_fired_minute = minute
            await _tick(schedule, tz, now)


_task: asyncio.Task | None = None


def start() -> None:
    """Start the in-app digest scheduler when ``EMAIL_DIGEST_CRON`` is set.

    No-op (and always safe) when the expression is empty, when digest mode is
    off, or when the expression is invalid. Must be called from within a
    running event loop (e.g. the FastAPI lifespan).
    """
    global _task
    if _task is not None and not _task.done():
        # Re-entry guard: a second start() (e.g. an accidental double lifespan)
        # must not orphan the first loop task — stop() can only cancel the one
        # in _task, so overwriting it would leak the original forever.
        logger.debug("Scheduler already running; start() is a no-op.")
        return
    settings = get_settings()
    expr = settings.EMAIL_DIGEST_CRON
    if not expr:
        return  # default: scheduling left to an external runner
    if not settings.EMAIL_DIGEST_ENABLED:
        logger.warning(
            "EMAIL_DIGEST_CRON is set but EMAIL_DIGEST_ENABLED is false — "
            "work-item emails send immediately, so the in-app scheduler "
            "stays idle."
        )
        return
    try:
        schedule = CronSchedule(expr)
    except (ValueError, TypeError):
        logger.exception(
            "Invalid EMAIL_DIGEST_CRON=%r — in-app scheduler disabled.", expr
        )
        return
    tz = _resolve_tz(settings.EMAIL_DIGEST_TIMEZONE)
    _task = asyncio.create_task(_loop(schedule, tz))
    logger.info("Email-digest scheduler started (cron=%r, tz=%s).", expr, tz)


async def stop() -> None:
    """Cancel the scheduler task on shutdown. Safe when never started."""
    global _task
    if _task is None:
        return
    _task.cancel()
    # gather(..., return_exceptions=True) waits for the task to finish
    # unwinding and captures its CancelledError as a result rather than
    # swallowing it — so a cancellation aimed at stop() itself still
    # propagates instead of being hidden.
    await asyncio.gather(_task, return_exceptions=True)
    _task = None
