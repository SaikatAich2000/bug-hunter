"""SQLAlchemy engine, session factory, and base class.

We use SQLAlchemy 2.x so the same models work on Postgres (production)
and SQLite (tests / local dev fallback).
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
        # check_same_thread=False so FastAPI can pass connections between
        # the request handler and dependency-injected helpers.
        eng = create_engine(
            url,
            connect_args={"check_same_thread": False},
            future=True,
        )

        # SQLite ships with FK enforcement off by default; enable it for every
        # new connection so ON DELETE CASCADE / SET NULL clauses fire. Without
        # this, deleting a user wouldn't clean up bug_assignees rows on SQLite.
        @event.listens_for(eng, "connect")
        def _enable_sqlite_fk(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            # WAL lets readers and a writer proceed concurrently (the default
            # rollback journal serializes them and a second writer fails fast
            # with "database is locked"); busy_timeout makes a contended write
            # wait briefly instead of erroring. Both are per-connection-safe and
            # only affect SQLite (dev/test) — prod is Postgres, untouched.
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA busy_timeout = 5000")
            # NORMAL is the recommended durability level under WAL: safe across
            # application crashes, materially faster on writes, and only weaker
            # than FULL in a power-loss/OS-crash window. SQLite-only (dev/test).
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.close()

        return eng

    # Postgres / others — Postgres enforces FKs natively. Use a small
    # connection pool that respects docker-compose start ordering via pre_ping.
    # Pool sizing is env-tunable: the defaults (5 + 10) suit a normal box, but a
    # very small target (0.1 CPU / 512 MB) can lower DB_POOL_SIZE / DB_MAX_OVERFLOW
    # so a traffic spike can't open 15 connections' worth of RAM at once.
    eng = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=_settings.DB_POOL_SIZE,
        max_overflow=_settings.DB_MAX_OVERFLOW,
        # Recycle connections older than 30 min so a proxy/load-balancer/Postgres
        # idle timeout can't hand a reaped connection back to the app, and fail
        # fast instead of blocking forever once the pool is exhausted.
        pool_recycle=1800,
        pool_timeout=30,
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _pg_utc_session(dbapi_conn, _):  # pragma: no cover - exercised only on PG
        # Pin the session time zone to UTC so func.date() / date truncation
        # happen in UTC, consistent with the Python-side .date() (also UTC) the
        # reports use. Without this, a server TZ != UTC silently shifts the
        # created-vs-resolved day buckets in the timeline report by a day.
        cur = dbapi_conn.cursor()
        cur.execute("SET TIME ZONE 'UTC'")
        cur.close()

    return eng


_settings = get_settings()
engine: Engine = _build_engine(_settings.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a session and always closes it.

    Session.close() rolls back any in-progress (uncommitted) transaction, so an
    exception on the request path can't hand a half-applied unit of work back to
    the connection pool — no explicit rollback (and no broad ``except``) needed.
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
    """Run one additive ALTER inside a SAVEPOINT so one table's failure can't
    roll back the other additive columns (mirrors _create_index_safely).
    Best-effort and idempotent: a column that already exists or a transient
    error is logged and skipped, not fatal to boot."""
    from sqlalchemy import text
    try:
        with conn.begin_nested():
            conn.execute(text(sql))
    except SQLAlchemyError:
        logger.warning("Additive column migration skipped (already applied or "
                       "failed): %s", sql)


def _add_missing_columns(conn) -> None:
    """ALTER-ADD any columns the model declares but an existing DB lacks.

    SQLAlchemy's create_all() never alters an existing table, so a column added
    in a later release would never appear on a populated database; this pass
    closes that gap. It is strictly additive — nothing is dropped, altered or
    renamed — and every new column is safe to add to a populated table
    (nullable, or NOT NULL with a column-level default so existing rows get a
    value at the DB level). Runs before the index pass since new composite
    indexes may reference these columns.
    """
    from sqlalchemy import inspect
    inspector = inspect(engine)

    bug_cols = _column_names(inspector, "bugs")
    if bug_cols and "item_type" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN item_type VARCHAR(20) "
            "NOT NULL DEFAULT 'Bug'")
    if bug_cols and "event_id" not in bug_cols:
        # Nullable + no FK constraint at the ALTER level — SQLite can't add FKs
        # to existing tables with ALTER TABLE anyway, and on Postgres a missing
        # FK constraint is still preferable to a blocking migration. The
        # session-level relationship enforces referential integrity in code.
        _add_column_safely(conn, "ALTER TABLE bugs ADD COLUMN event_id INTEGER")

    # bugs.version — optimistic-concurrency counter. NOT NULL with a default so
    # every existing row gets 1 at the DB level without a backfill pass.
    if bug_cols and "version" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")

    # notifications.emailed_at — nullable timestamp; existing rows get NULL,
    # and the digest job's lookback window keeps that historical backlog from
    # being replayed. tz-aware type on Postgres to match the ORM column; SQLite
    # is dynamically typed so a plain TIMESTAMP is fine there.
    notif_cols = _column_names(inspector, "notifications")
    if notif_cols and "emailed_at" not in notif_cols:
        tstype = (
            "TIMESTAMP WITH TIME ZONE"
            if engine.dialect.name == "postgresql"
            else "TIMESTAMP"
        )
        _add_column_safely(conn,
            f"ALTER TABLE notifications ADD COLUMN emailed_at {tstype}")

    # events.project_id — optional owning project for project-scoped access.
    # Nullable + no FK at the ALTER level (SQLite can't add FKs to an existing
    # table via ALTER, and on Postgres a missing FK constraint beats a blocking
    # migration). The ORM relationship and route-layer validation enforce
    # referential integrity in code. Existing events keep project_id NULL, so
    # they stay visible to admins only until an admin assigns a project.
    event_cols = _column_names(inspector, "events")
    if event_cols and "project_id" not in event_cols:
        _add_column_safely(conn, "ALTER TABLE events ADD COLUMN project_id INTEGER")


def _add_missing_indexes(conn) -> None:
    """CREATE any indexes the model declares but the DB lacks (idempotent).

    This is what makes new composite indexes show up on an upgraded DB.
    Re-introspects so it sees any columns the column pass just added.
    """
    from sqlalchemy import inspect
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        try:
            existing = {idx["name"] for idx in inspector.get_indexes(table.name)}
        except SQLAlchemyError:
            # Table absent for any reason → nothing to compare against; skip.
            continue
        for idx in table.indexes:
            if idx.name and idx.name not in existing:
                _create_index_safely(conn, idx, table.name)


def _create_index_safely(conn, idx, table_name: str) -> None:
    """Create one missing index inside a SAVEPOINT so a single failure can't
    abort the whole index pass (and crash boot).

    The only realistic additive failure is adding a UNIQUE index to a table that
    already holds duplicate rows. We don't want that to take the app down on a
    later release: roll back just this index, log loudly, and keep going. The
    non-unique queries still work; an operator can dedupe and reboot to get the
    constraint. SAVEPOINT is honoured by both Postgres and SQLite.
    """
    try:
        with conn.begin_nested():
            # SQLAlchemy's own DDL emits dialect-correct CREATE INDEX.
            idx.create(bind=conn, checkfirst=True)
    except SQLAlchemyError:
        logger.warning(
            "Could not create index %s on %s — likely pre-existing duplicate "
            "data for a unique index. Skipping; resolve the duplicates and "
            "reboot to add it.", idx.name, table_name,
        )


def init_db() -> None:
    """Create tables if they don't exist, then add any missing columns and
    indexes the model declares. Idempotent and safe to call on every boot.

    SQLAlchemy's `create_all()` skips tables that already exist (and their
    columns/indexes), so columns/indexes added in a later release would never
    appear on a long-running database. The two follow-up passes close that gap.
    This is strictly additive: nothing is dropped, altered or renamed, and
    existing data is never touched.
    """
    # Local import avoids circular import at module load.
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        _add_missing_columns(conn)
    with engine.begin() as conn:
        _add_missing_indexes(conn)
