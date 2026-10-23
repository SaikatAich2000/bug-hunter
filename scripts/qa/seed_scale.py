"""Seed a large synthetic dataset directly into the database, for query-performance testing.

    DATABASE_URL=postgresql+psycopg://... python scripts/qa/seed_scale.py [users projects items]

Defaults: 500 users, 50 projects, 100000 work items, 2 comments per item. The first user is an admin,
`scale-admin-0@example.test`, password below. Runs init_db() first. Only for throwaway databases.
"""
from __future__ import annotations

import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import insert  # noqa: E402

from app import models as m  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.models import user_projects  # noqa: E402

PASSWORD = "Scale-Test-9xYz!"
USERS, PROJECTS, ITEMS = (int(a) for a in (sys.argv[1:4] + ["500", "50", "100000"][len(sys.argv[1:4]):]))
rng = random.Random(7)
now = datetime.now(UTC)
WORDS = "login crash export slow timeout upload sprint board filter search report email webhook api cache index mobile".split()
STATUS = ["New", "In Progress", "Testing", "Done", "Blocked", "Closed"]
PRIORITY = ["Low", "Medium", "High", "Critical"]
TYPES = ["Bug", "Task", "Requirement"]


def rows(model, items):
    """Keep only the keys the table has, so the script follows small model changes."""
    cols = {c.name for c in model.__table__.columns}
    return [{k: v for k, v in item.items() if k in cols} for item in items]


def chunks(items, n=2000):
    for i in range(0, len(items), n):
        yield items[i:i + n]


init_db()
db = SessionLocal()
pw = hash_password(PASSWORD)
db.execute(insert(m.User), rows(m.User, [{
    "name": f"User {i}", "email": f"scale-{'admin' if i == 0 else 'user'}-{i}@example.test", "role": "admin" if i == 0 else "user",
    "is_active": True, "password_hash": pw, "session_version": 0, "totp_enabled": False} for i in range(USERS)]))
db.commit()
user_ids = [r[0] for r in db.query(m.User.id).filter(m.User.email.like("scale-%"))]

project_ids = []
for i in range(PROJECTS):
    p = m.Project(**rows(m.Project, [{"name": f"Scale Project {i}", "key": f"S{i}", "description": "", "color": "#c9764f"}])[0])
    db.add(p)
    db.flush()
    project_ids.append(p.id)
db.commit()

links = [{"user_id": u, "project_id": pid, "role": "member"} for u in user_ids for pid in rng.sample(project_ids, min(3, len(project_ids)))]
for batch in chunks(links):
    db.execute(insert(user_projects), batch)
db.commit()

items = []
for i in range(ITEMS):
    when = now - timedelta(days=rng.randint(0, 400), minutes=rng.randint(0, 1440))
    items.append({"project_id": rng.choice(project_ids), "reporter_id": rng.choice(user_ids),
                  "title": f"{rng.choice(WORDS)} {rng.choice(WORDS)} problem {i}", "description": "<p>" + " ".join(rng.choices(WORDS, k=20)) + "</p>",
                  "item_type": rng.choice(TYPES), "status": rng.choice(STATUS), "priority": rng.choice(PRIORITY), "environment": rng.choice(["DEV", "UAT", "PROD"]),
                  "created_at": when, "updated_at": when})
for batch in chunks(rows(m.Bug, items)):
    db.execute(insert(m.Bug), batch)
db.commit()

bug_ids = [r[0] for r in db.query(m.Bug.id)]
comments = [{"bug_id": b, "author_name": "Scale User", "body": "<p>" + " ".join(rng.choices(WORDS, k=12)) + "</p>", "created_at": now - timedelta(days=rng.randint(0, 300))}
            for b in bug_ids for _ in range(2)]
for batch in chunks(rows(m.Comment, comments), 5000):
    db.execute(insert(m.Comment), batch)
db.commit()
print(f"seeded: {USERS} users, {PROJECTS} projects, {ITEMS} items, {len(comments)} comments")
