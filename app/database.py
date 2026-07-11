"""SQLAlchemy engine, session factory, and base class.

Same models run on Postgres (prod) and SQLite (tests / local dev).
"""
from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger("bug_hunter.database")


class Base(DeclarativeBase):
    """Base class for all ORM models."""


def _build_engine(url: str) -> Engine:
    """Create an engine with sensible per-backend tweaks."""
    if url.startswith("sqlite"):
        # check_same_thread=False: FastAPI shares connections across threads.
        eng = create_engine(
            url,
            connect_args={"check_same_thread": False},
            future=True,
        )

        # SQLite defaults FKs off; enable so CASCADE / SET NULL fire.
        @event.listens_for(eng, "connect")
        def _enable_sqlite_fk(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            # WAL + busy_timeout: concurrent readers/writer, wait instead of
            # "database is locked". SQLite (dev/test) only.
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA busy_timeout = 5000")
            # NORMAL is the recommended durability level under WAL.
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.close()

        return eng

    # Postgres: env-tunable pool; pre_ping tolerates docker-compose start order.
    eng = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=_settings.DB_POOL_SIZE,
        max_overflow=_settings.DB_MAX_OVERFLOW,
        # Recycle before proxy/idle timeouts reap; fail fast on pool exhaustion.
        pool_recycle=1800,
        pool_timeout=30,
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _pg_utc_session(dbapi_conn, _):  # pragma: no cover - exercised only on PG
        # UTC session TZ keeps func.date() consistent with Python-side .date();
        # otherwise timeline day buckets can shift by a day.
        cur = dbapi_conn.cursor()
        cur.execute("SET TIME ZONE 'UTC'")
        cur.close()

    return eng


_settings = get_settings()
engine: Engine = _build_engine(_settings.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yield a session, always close it.

    close() rolls back uncommitted work, so no half-applied write leaks back
    to the pool.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _column_names(inspector, table: str) -> set[str]:
    """Existing column names for `table`, or empty set if it doesn't exist."""
    try:
        return {c["name"] for c in inspector.get_columns(table)}
    except SQLAlchemyError:
        return set()


def _add_column_safely(conn, sql: str) -> None:
    """Run one additive ALTER inside a SAVEPOINT; failures are logged, not fatal."""
    from sqlalchemy import text
    try:
        with conn.begin_nested():
            conn.execute(text(sql))
    except SQLAlchemyError:
        logger.warning("Additive column migration skipped (already applied or "
                       "failed): %s", sql)


def _add_missing_columns(conn) -> None:
    """ALTER-ADD columns the model declares that an existing DB lacks.

    create_all() never alters existing tables; each new column is nullable or
    defaulted so no backfill is needed. Runs before the index pass.
    """
    from sqlalchemy import inspect
    inspector = inspect(engine)

    bug_cols = _column_names(inspector, "bugs")
    if bug_cols and "item_type" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN item_type VARCHAR(20) "
            "NOT NULL DEFAULT 'Bug'")
    if bug_cols and "event_id" not in bug_cols:
        # No FK at ALTER level (SQLite can't add one); ORM enforces integrity.
        _add_column_safely(conn, "ALTER TABLE bugs ADD COLUMN event_id INTEGER")

    # bugs.version — optimistic-concurrency counter; DEFAULT 1, no backfill.
    if bug_cols and "version" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")

    # notifications.emailed_at — nullable; digest lookback window skips the
    # historical NULL backlog. tz-aware type on Postgres to match the ORM.
    notif_cols = _column_names(inspector, "notifications")
    if notif_cols and "emailed_at" not in notif_cols:
        tstype = (
            "TIMESTAMP WITH TIME ZONE"
            if engine.dialect.name == "postgresql"
            else "TIMESTAMP"
        )
        _add_column_safely(conn,
            f"ALTER TABLE notifications ADD COLUMN emailed_at {tstype}")

    # events.project_id — optional owning project; nullable, no FK at ALTER
    # level (see event_id note). NULL rows stay admin-only until assigned.
    event_cols = _column_names(inspector, "events")
    if event_cols and "project_id" not in event_cols:
        _add_column_safely(conn, "ALTER TABLE events ADD COLUMN project_id INTEGER")


def _add_missing_indexes(conn) -> None:
    """CREATE indexes the model declares that the DB lacks (idempotent)."""
    from sqlalchemy import inspect
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        try:
            existing = {idx["name"] for idx in inspector.get_indexes(table.name)}
        except SQLAlchemyError:
            # Table absent → nothing to compare against.
            continue
        for idx in table.indexes:
            if idx.name and idx.name not in existing:
                _create_index_safely(conn, idx, table.name)


def _create_index_safely(conn, idx, table_name: str) -> None:
    """Create one missing index inside a SAVEPOINT.

    A failure (e.g. UNIQUE index over pre-existing duplicates) is logged and
    skipped rather than crashing boot.
    """
    try:
        with conn.begin_nested():
            idx.create(bind=conn, checkfirst=True)
    except SQLAlchemyError:
        logger.warning(
            "Could not create index %s on %s — likely pre-existing duplicate "
            "data for a unique index. Skipping; resolve the duplicates and "
            "reboot to add it.", idx.name, table_name,
        )


def init_db() -> None:
    """Create missing tables, then missing columns and indexes.

    Idempotent on every boot; nothing is dropped, renamed, or altered.
    """
    # Local import avoids a circular import at module load.
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        _add_missing_columns(conn)
    with engine.begin() as conn:
        _add_missing_indexes(conn)
