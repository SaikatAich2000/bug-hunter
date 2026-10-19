"""Pins the SQL statements and transactions of the hot read endpoints (load-test paths).

Every request used to repeat lookups (user, session, org...) and open extra
transactions; at 100 users on one CPU that per-request cost is the throughput limit.
"""
from __future__ import annotations

import pytest
from sqlalchemy import event


class _Counter:
    def __init__(self, engine):
        self.engine, self.sql, self.begins = engine, [], 0

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._exec)
        event.listen(self.engine, "begin", self._begin)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._exec)
        event.remove(self.engine, "begin", self._begin)

    def _exec(self, conn, cursor, statement, *a):
        self.sql.append(statement)

    def _begin(self, conn):
        self.begins += 1


@pytest.fixture
def counted(admin_client):
    from app.database import engine

    for i in range(20):
        r = admin_client.post("/api/bugs", json={"project_id": 1, "title": f"item {i}", "item_type": "Bug"})
        assert r.status_code == 201, r.text
    # warm: first calls may bump last_seen / fill caches
    for path in ("/api/auth/me", "/api/projects", "/api/bugs", "/api/stats"):
        assert admin_client.get(path).status_code == 200

    def run(path):
        with _Counter(engine) as c:
            assert admin_client.get(path).status_code == 200
        return c

    return run


@pytest.mark.parametrize("path,max_sql", [
    ("/api/auth/me", 1), ("/api/projects", 2), ("/api/bugs", 4), ("/api/stats", 6),
])
def test_hot_endpoint_sql_budget(counted, path, max_sql):
    c = counted(path)
    assert len(c.sql) <= max_sql, "\n".join(s[:150] for s in c.sql)
    assert c.begins <= 1, f"{c.begins} transactions"

