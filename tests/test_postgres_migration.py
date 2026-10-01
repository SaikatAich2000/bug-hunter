"""PostgreSQL-only migration regressions (skipped unless BH_TEST_POSTGRES_URL is set).

BH_TEST_POSTGRES_URL is an admin URL (a role with CREATEDB), e.g.
    postgresql+psycopg://postgres:<password>@127.0.0.1:5432/postgres
Each test creates its own temporary database and drops it afterwards; no
existing database is touched.

Regression: the generic column pass used to inspect through a separate
connection. After the per-table helpers had ALTERed `bugs` inside the
migration transaction, that connection hit lock_timeout, the error was
swallowed as "no columns", and every column without its own helper was
skipped silently on upgrade.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ADMIN_URL = os.environ.get("BH_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="set BH_TEST_POSTGRES_URL to run PostgreSQL migration tests")
ROOT = Path(__file__).resolve().parents[1]

# Old-schema simulation: the tables exist but columns added after 3.x are
# missing, and a 3.x rich-text description is present.
PREPARE = r"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.database import Base
from app.models import Bug, Project
eng = create_engine(URL)
Base.metadata.create_all(eng)
with Session(eng) as db:
    project = Project(name="Old", description="", color="#c9764f")
    db.add(project)
    db.flush()
    db.add(Bug(project_id=project.id, title="Old item", description="<p>Old <b>rich</b> text</p>"))
    db.commit()
with eng.begin() as c:
    c.execute(text("ALTER TABLE bugs DROP COLUMN display_id"))
    c.execute(text("ALTER TABLE bugs DROP COLUMN description_legacy_html"))
    c.execute(text("ALTER TABLE boards DROP COLUMN display_id"))
    c.execute(text("DROP INDEX IF EXISTS idx_bugs_project_updated_id"))
"""

MIGRATE_AND_CHECK = r"""
import logging, sys
logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="LOG %(levelname)s %(message)s")
from sqlalchemy import inspect, text
from app.database import Base, engine, init_db
assert init_db() is True
insp = inspect(engine)
problems = []
for t in Base.metadata.sorted_tables:
    have = {c["name"] for c in insp.get_columns(t.name)}
    problems += [f"{t.name}.{c.name}" for c in t.columns if c.name not in have]
    idx = {i["name"] for i in insp.get_indexes(t.name)}
    problems += [f"index {i.name}" for i in t.indexes if i.name and i.name not in idx]
with engine.connect() as c:
    legacy = c.execute(text("SELECT description_legacy_html FROM bugs")).scalar()
print("PROBLEMS", problems)
print("LEGACY", legacy)
"""


def _run(code: str, db_url: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": db_url, "APP_VERSION": "0.0.0-test", "PYTHONUTF8": "1",
           "OTEL_EXPORTER_OTLP_ENDPOINT": ""}
    return subprocess.run([sys.executable, "-c", f"URL = {db_url!r}\n" + code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=300)


@pytest.fixture
def scratch_db():
    from sqlalchemy import create_engine, text
    name = f"bh_migtest_{uuid.uuid4().hex[:10]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    base, _, _ = ADMIN_URL.rpartition("/")
    try:
        yield f"{base}/{name}"
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_upgrade_adds_every_missing_column_and_index(scratch_db):
    prep = _run(PREPARE, scratch_db)
    assert prep.returncode == 0, prep.stderr[-2000:]
    out = _run(MIGRATE_AND_CHECK, scratch_db)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "PROBLEMS []" in out.stdout, out.stdout
    assert "LEGACY <p>Old <b>rich</b> text</p>" in out.stdout, out.stdout
    assert "LOG WARNING" not in out.stdout and "LOG ERROR" not in out.stdout, out.stdout


# Legacy agile data (Collections, Features, sub-tasks without a valid parent,
# epics in sprints) written before the Jira-hierarchy upgrade, then two full
# boots: the upgrade SQL must run on PostgreSQL and change nothing the second
# time.
AGILE_LEGACY = r"""
import sys
sys.path.insert(0, ROOT)
import app.models  # noqa: F401  (registers the tables on Base.metadata)
from app.database import Base, engine, init_db
from tests.test_agile_upgrade import _legacy_world, assert_aligned
Base.metadata.create_all(engine)
with engine.begin() as conn:
    world = _legacy_world(conn)
for _ in range(2):
    assert init_db() is True
    with engine.connect() as conn:
        assert_aligned(conn, world)
print("ALIGNED")
"""


def test_agile_upgrade_aligns_legacy_data_on_postgres(scratch_db):
    out = _run(f"ROOT = {str(ROOT)!r}\n" + AGILE_LEGACY, scratch_db)
    assert out.returncode == 0, (out.stdout + out.stderr)[-3000:]
    assert "ALIGNED" in out.stdout, out.stdout
    assert "LOG ERROR" not in out.stdout, out.stdout
