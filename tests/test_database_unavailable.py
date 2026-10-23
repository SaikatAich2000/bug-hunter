"""A lost or exhausted database answers 503 with Retry-After (a temporary condition), never a bare 500."""
from __future__ import annotations

import pytest
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError


def _boom(exc):
    def route():
        raise exc

    return route


@pytest.mark.parametrize("exc", [
    OperationalError("SELECT 1", {}, Exception("connection refused")),
    InterfaceError("SELECT 1", {}, Exception("connection already closed")),
    PoolTimeoutError("QueuePool limit reached"),
], ids=["operational", "interface", "pool"])
def test_database_outage_is_a_503_with_retry_after(client, exc):
    from app.main import app

    app.add_api_route("/api/__db_down", _boom(exc), methods=["GET"])
    try:
        res = client.get("/api/__db_down")
    finally:
        app.router.routes.pop()
    assert res.status_code == 503
    assert res.headers["retry-after"] == "5"
    assert res.headers["x-content-type-options"] == "nosniff"
    body = res.json()
    assert "temporarily unavailable" in body["detail"] and body["request_id"]
    assert "connection" not in res.text.lower().replace("temporarily", "")  # no driver detail leaks
