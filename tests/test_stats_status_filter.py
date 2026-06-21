"""GET /api/stats?status=... scopes the chart breakdowns but keeps the headline
KPI counts global. This lets the Analytics view filter in place when a KPI tile
is clicked without the KPI strip numbers collapsing to the filtered subset, so
every tile stays visible and toggleable.
"""
from __future__ import annotations

_BOOTSTRAP_EMAIL = "admin@test.local"
_BOOTSTRAP_PW = "Admin1234"


def _admin(c) -> None:
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login",
                  json={"email": _BOOTSTRAP_EMAIL, "password": _BOOTSTRAP_PW}).status_code == 200


def _project(c) -> int:
    return c.post("/api/projects", json={"name": "Stats Filter Proj"}).json()["id"]


def _bug(c, pid, status, priority="Medium") -> dict:
    r = c.post("/api/bugs", json={
        "project_id": pid, "title": f"{status} bug",
        "priority": priority, "environment": "DEV",
    })
    assert r.status_code == 201, r.text
    bug = r.json()
    if status != "New":
        u = c.put(f"/api/bugs/{bug['id']}", json={"status": status})
        assert u.status_code == 200, u.text
    return bug


def test_status_filter_scopes_charts_but_not_kpis(client):
    _admin(client)
    pid = _project(client)
    # 2 open (New) + 1 resolved.
    _bug(client, pid, "New", priority="High")
    _bug(client, pid, "New", priority="Low")
    _bug(client, pid, "Resolved", priority="High")

    glob = client.get("/api/stats").json()
    # Sanity: global KPIs see all three.
    assert glob["bugs"] >= 3
    assert glob["open"] >= 2
    assert glob["resolved"] >= 1

    # Filter the charts to the "open" statuses.
    filt = client.get("/api/stats", params={"status": ["New", "In Progress", "Reopened"]}).json()

    # KPI counts stay global (unchanged) so the strip remains toggleable.
    assert filt["bugs"] == glob["bugs"]
    assert filt["open"] == glob["open"]
    assert filt["resolved"] == glob["resolved"]

    # ...but the by_status chart now only contains open statuses.
    assert "Resolved" not in filt["by_status"], filt["by_status"]
    assert filt["by_status"].get("New", 0) >= 2

    # ...and the priority breakdown reflects only the open subset: the lone
    # High-priority Resolved bug is excluded, so High drops vs the global view.
    assert filt["by_priority"].get("Low", 0) >= 1
    assert filt["by_priority"].get("High", 0) == glob["by_priority"].get("High", 0) - 1


def test_status_filter_empty_is_same_as_unfiltered(client):
    _admin(client)
    pid = _project(client)
    _bug(client, pid, "New")
    base = client.get("/api/stats").json()
    # A blank status value is ignored (normalized away), matching the global view.
    blanked = client.get("/api/stats", params={"status": [""]}).json()
    assert blanked["by_status"] == base["by_status"]
