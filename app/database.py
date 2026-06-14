"""SQLAlchemy engine, session factory, and base class.

We use SQLAlchemy 2.x so the same models work on Postgres (production)
and SQLite (tests / local dev fallback).
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


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

        # SQLite ships with FK enforcement OFF by default. We turn it on
        # for every new connection so ON DELETE CASCADE / SET NULL clauses
        # actually fire — without this, deleting a user wouldn't clean up
        # bug_assignees rows on SQLite.
        @event.listens_for(eng, "connect")
        def _enable_sqlite_fk(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()

        return eng

    # Postgres / others — Postgres enforces FKs natively. Use a small
    # connection pool that respects docker-compose start ordering via pre_ping.
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )


_settings = get_settings()
engine: Engine = _build_engine(_settings.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a session and always closes it."""
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


def _add_missing_columns(conn) -> None:
    """ALTER-ADD any NEW columns the model declares but an existing DB lacks.

    SQLAlchemy's create_all() never alters an existing table, so a column
    added in a later release would never appear on a production database. This
    pass closes that gap. It is strictly ADDITIVE — nothing is dropped, altered
    or renamed — and every new column is safe to add to a populated table
    (NULLABLE, or NOT NULL with a column-level DEFAULT so existing rows get a
    value at the DB level). Runs BEFORE the index pass since new composite
    indexes may reference these columns.
    """
    from sqlalchemy import inspect, text
    inspector = inspect(engine)

    bug_cols = _column_names(inspector, "bugs")
    if bug_cols and "item_type" not in bug_cols:
        conn.execute(text(
            "ALTER TABLE bugs ADD COLUMN item_type VARCHAR(20) "
            "NOT NULL DEFAULT 'Bug'"
        ))
    if bug_cols and "event_id" not in bug_cols:
        # Nullable + no FK constraint at the ALTER level — SQLite can't add FKs
        # to existing tables with ALTER TABLE anyway, and on Postgres a missing
        # FK constraint is still preferable to a blocking migration. The
        # session-level relationship enforces referential integrity in code.
        conn.execute(text("ALTER TABLE bugs ADD COLUMN event_id INTEGER"))

    # notifications.emailed_at — added in the daily-email-digest release.
    # Nullable timestamp; existing rows get NULL, and the digest job's lookback
    # window keeps that historical backlog from ever being replayed. tz-aware
    # type on Postgres to match the ORM column; SQLite is dynamically typed so
    # a plain TIMESTAMP is fine there.
    notif_cols = _column_names(inspector, "notifications")
    if notif_cols and "emailed_at" not in notif_cols:
        tstype = (
            "TIMESTAMP WITH TIME ZONE"
            if engine.dialect.name == "postgresql"
            else "TIMESTAMP"
        )
        conn.execute(text(
            f"ALTER TABLE notifications ADD COLUMN emailed_at {tstype}"
        ))


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
                # SQLAlchemy's own DDL emits dialect-correct CREATE INDEX.
                idx.create(bind=conn, checkfirst=True)


def init_db() -> None:
    """Create tables if they don't exist, then ADD any missing columns and
    indexes the model declares. Idempotent — safe to call on every boot.

    SQLAlchemy's `create_all()` skips tables that already exist (and their
    columns/indexes), so columns/indexes added in a later release would never
    appear on a long-running production database. The two follow-up passes
    close that gap. This is strictly ADDITIVE: nothing is dropped, altered or
    renamed, and existing data is never touched.
    """
    # Local import avoids circular import at module load.
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        _add_missing_columns(conn)
    with engine.begin() as conn:
        _add_missing_indexes(conn)
