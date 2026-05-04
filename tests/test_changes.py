"""Tests for the changes made in this release:

  1. Two new statuses: "Not a Bug" and "Resolve Later"
  2. KPI strip switched to: total / open / resolved / closed / resolve_later
     (with `users` and `projects` kept for backward compat).
  3. Multi-select filters via repeated query params (?status=A&status=B).
  4. "Not a Bug" rows are EXCLUDED from the `bugs` total.
  5. The list endpoint serves the page in a single attachment-count query
     instead of N+1 queries.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers (mirror style of existing test_fixes/test_regression helpers)
# ---------------------------------------------------------------------------
def _make_project(client, name="P-status"):
    r = client.post("/api/projects", json={"name": name, "color": "#abcdef"})
    assert r.status_code == 201, r.text
    return r.json()


def _make_bug(client, project_id, status="New", title="A test bug", priority="Medium"):
    payload = {
        "project_id": project_id,
        "title": title,
        "description": "x",
        "status": status,
        "priority": priority,
        "environment": "DEV",
    }
    r = client.post("/api/bugs", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ===========================================================================
# 1. New statuses
# ===========================================================================
class TestNewStatuses:
    def test_meta_lists_both_new_statuses(self, admin_client):
        r = admin_client.get("/api/meta")
        assert r.status_code == 200
        statuses = r.json()["statuses"]
        assert "Not a Bug" in statuses
        assert "Resolve Later" in statuses

    def test_can_create_bug_with_not_a_bug(self, admin_client):
        p = _make_project(admin_client, "NS-1")
        bug = _make_bug(admin_client, p["id"], status="Not a Bug",
                        title="false alarm")
        assert bug["status"] == "Not a Bug"

    def test_can_create_bug_with_resolve_later(self, admin_client):
        p = _make_project(admin_client, "NS-2")
        bug = _make_bug(admin_client, p["id"], status="Resolve Later",
                        title="defer me")
        assert bug["status"] == "Resolve Later"

    def test_status_filter_finds_resolve_later(self, admin_client):
        p = _make_project(admin_client, "NS-3")
        _make_bug(admin_client, p["id"], status="Resolve Later", title="parked")
        r = admin_client.get("/api/bugs?status=Resolve%20Later")
        items = r.json()["items"]
        assert any(b["title"] == "parked" for b in items)

    def test_status_filter_case_insensitive_for_new_statuses(self, admin_client):
        p = _make_project(admin_client, "NS-4")
        _make_bug(admin_client, p["id"], status="Not a Bug", title="oops")
        # Passing lowercase still matches because we normalize-then-validate.
        r = admin_client.get("/api/bugs?status=not%20a%20bug")
        items = r.json()["items"]
        assert any(b["title"] == "oops" for b in items)


# ===========================================================================
# 2. KPI / Stats response shape
# ===========================================================================
class TestStatsShape:
    def test_stats_includes_new_kpi_fields(self, admin_client):
        r = admin_client.get("/api/stats")
        assert r.status_code == 200
        body = r.json()
        # New KPI fields the dashboard renders today.
        for key in ("bugs", "open", "resolved", "closed", "resolve_later"):
            assert key in body, f"missing KPI field: {key}"
        # Backward-compat fields — still there so an older cached frontend
        # doesn't blow up.
        assert "users" in body
        assert "projects" in body

    def test_resolve_later_kpi_increments(self, admin_client):
        before = admin_client.get("/api/stats").json()["resolve_later"]
        p = _make_project(admin_client, "KP-RL")
        _make_bug(admin_client, p["id"], status="Resolve Later", title="rl-bug")
        after = admin_client.get("/api/stats").json()["resolve_later"]
        assert after == before + 1

    def test_closed_kpi_increments(self, admin_client):
        before = admin_client.get("/api/stats").json()["closed"]
        p = _make_project(admin_client, "KP-CL")
        _make_bug(admin_client, p["id"], status="Closed", title="closed-bug")
        after = admin_client.get("/api/stats").json()["closed"]
        assert after == before + 1

    def test_not_a_bug_excluded_from_total(self, admin_client):
        """The product clarified: 'Not a Bug' shouldn't count toward the
        Total KPI. We still keep the row in the DB (audit trail), we just
        omit it from the headline number."""
        before = admin_client.get("/api/stats").json()["bugs"]
        p = _make_project(admin_client, "KP-NB")
        _make_bug(admin_client, p["id"], status="Not a Bug", title="nope")
        after = admin_client.get("/api/stats").json()["bugs"]
        assert after == before, \
            "'Not a Bug' rows must NOT increase the bugs total"

    def test_not_a_bug_still_shows_in_by_status_breakdown(self, admin_client):
        """Excluded from the headline total but still in the per-status
        breakdown so the analytics page can show the bucket."""
        p = _make_project(admin_client, "KP-NB2")
        _make_bug(admin_client, p["id"], status="Not a Bug", title="invalid-1")
        body = admin_client.get("/api/stats").json()
        assert body["by_status"].get("Not a Bug", 0) >= 1

    def test_resolved_kpi_is_just_resolved_status(self, admin_client):
        """`resolved` used to mean (Resolved + Closed). New contract:
        `resolved` is JUST the Resolved status; Closed has its own KPI."""
        p = _make_project(admin_client, "KP-RES")
        _make_bug(admin_client, p["id"], status="Resolved", title="resolved-1")
        _make_bug(admin_client, p["id"], status="Closed", title="closed-1")
        body = admin_client.get("/api/stats").json()
        # by_status is the source of truth for per-bucket counts.
        assert body["resolved"] == body["by_status"].get("Resolved", 0)
        assert body["closed"] == body["by_status"].get("Closed", 0)


# ===========================================================================
# 3. Multi-select filters
# ===========================================================================
class TestMultiSelectFilters:
    def test_repeat_status_param_is_or_match(self, admin_client):
        p = _make_project(admin_client, "MS-1")
        _make_bug(admin_client, p["id"], status="New",      title="ms-new")
        _make_bug(admin_client, p["id"], status="Resolved", title="ms-resolved")
        _make_bug(admin_client, p["id"], status="Closed",   title="ms-closed")

        r = admin_client.get("/api/bugs?status=New&status=Resolved")
        titles = {b["title"] for b in r.json()["items"]}
        assert "ms-new" in titles
        assert "ms-resolved" in titles
        assert "ms-closed" not in titles

    def test_repeat_priority_param_is_or_match(self, admin_client):
        p = _make_project(admin_client, "MS-2")
        _make_bug(admin_client, p["id"], priority="High",     title="hi-bug")
        _make_bug(admin_client, p["id"], priority="Critical", title="crit-bug")
        _make_bug(admin_client, p["id"], priority="Low",      title="low-bug")

        r = admin_client.get("/api/bugs?priority=High&priority=Critical")
        titles = {b["title"] for b in r.json()["items"]}
        assert "hi-bug" in titles and "crit-bug" in titles
        assert "low-bug" not in titles

    def test_repeat_environment_param(self, admin_client):
        p = _make_project(admin_client, "MS-3")
        # Bugs default to DEV; can override via PUT.
        b1 = _make_bug(admin_client, p["id"], title="env-dev")
        b2 = _make_bug(admin_client, p["id"], title="env-uat")
        admin_client.put(f"/api/bugs/{b2['id']}", json={"environment": "UAT"})
        b3 = _make_bug(admin_client, p["id"], title="env-prod")
        admin_client.put(f"/api/bugs/{b3['id']}", json={"environment": "PROD"})

        r = admin_client.get("/api/bugs?environment=UAT&environment=PROD")
        titles = {b["title"] for b in r.json()["items"]}
        assert "env-uat" in titles and "env-prod" in titles
        assert "env-dev" not in titles

    def test_repeat_project_id_param(self, admin_client):
        p1 = _make_project(admin_client, "MS-P1")
        p2 = _make_project(admin_client, "MS-P2")
        p3 = _make_project(admin_client, "MS-P3")
        _make_bug(admin_client, p1["id"], title="bug-in-p1")
        _make_bug(admin_client, p2["id"], title="bug-in-p2")
        _make_bug(admin_client, p3["id"], title="bug-in-p3")

        r = admin_client.get(f"/api/bugs?project_id={p1['id']}&project_id={p2['id']}")
        titles = {b["title"] for b in r.json()["items"]}
        assert "bug-in-p1" in titles and "bug-in-p2" in titles
        assert "bug-in-p3" not in titles

    def test_single_value_still_works(self, admin_client):
        """Backward compat: ?status=New (one value) parses into a 1-element
        list and the IN clause still matches."""
        p = _make_project(admin_client, "MS-4")
        _make_bug(admin_client, p["id"], status="New", title="single-new")
        r = admin_client.get("/api/bugs?status=New")
        titles = {b["title"] for b in r.json()["items"]}
        assert "single-new" in titles

    def test_invalid_value_in_multi_returns_400(self, admin_client):
        # Even one bogus value in the list should fail the request — we
        # don't want to silently drop garbage.
        r = admin_client.get("/api/bugs?status=New&status=Bogus")
        assert r.status_code == 400

    def test_empty_filter_strings_ignored(self, admin_client):
        """The SPA may send blank values when nothing's selected; those
        should be ignored, not 400'd."""
        p = _make_project(admin_client, "MS-5")
        _make_bug(admin_client, p["id"], title="empty-ok")
        r = admin_client.get("/api/bugs?status=&priority=")
        assert r.status_code == 200
        titles = {b["title"] for b in r.json()["items"]}
        assert "empty-ok" in titles


# ===========================================================================
# 4. Live-data safety — schema additions don't break existing rows
# ===========================================================================
class TestBackwardCompat:
    def test_existing_bug_with_old_status_still_listable(self, admin_client):
        """Any pre-existing bug stored with one of the original 5 statuses
        must continue to round-trip cleanly through the new validator."""
        p = _make_project(admin_client, "BC-1")
        for s in ("New", "In Progress", "Resolved", "Closed", "Reopened"):
            _make_bug(admin_client, p["id"], status=s, title=f"bc-{s}")
        r = admin_client.get(f"/api/bugs?project_id={p['id']}")
        assert r.status_code == 200
        statuses = {b["status"] for b in r.json()["items"]}
        assert statuses == {"New", "In Progress", "Resolved", "Closed", "Reopened"}

    def test_old_single_status_query_unchanged(self, admin_client):
        # The exact query format the old SPA used should still work.
        p = _make_project(admin_client, "BC-2")
        _make_bug(admin_client, p["id"], status="In Progress", title="in-prog")
        r = admin_client.get("/api/bugs?status=In%20Progress")
        assert r.status_code == 200
        assert any(b["title"] == "in-prog" for b in r.json()["items"])


# ===========================================================================
# 5. Performance — attachment-count is a single aggregate query
# ===========================================================================
class TestAttachmentCountPerf:
    def test_list_bugs_returns_correct_attachment_counts(self, admin_client):
        """Functional check that the new aggregate-query path still reports
        the right attachment counts (the perf optimisation must not change
        the response semantics)."""
        p = _make_project(admin_client, "PERF-1")
        b1 = _make_bug(admin_client, p["id"], title="att-bug")
        b2 = _make_bug(admin_client, p["id"], title="no-att-bug")
        # Upload two attachments to b1.
        for fname in ("a.txt", "b.txt"):
            files = {"file": (fname, b"hello world", "text/plain")}
            r = admin_client.post(f"/api/bugs/{b1['id']}/attachments", files=files)
            assert r.status_code == 201

        r = admin_client.get(f"/api/bugs?project_id={p['id']}")
        items = {b["title"]: b for b in r.json()["items"]}
        assert items["att-bug"]["attachment_count"] == 2
        assert items["no-att-bug"]["attachment_count"] == 0

    def test_list_bugs_uses_single_count_query(self, admin_client):
        """Belt-and-braces perf assertion: the listing endpoint should
        execute roughly a constant number of queries regardless of how
        many bugs are returned. We instrument the SQLAlchemy engine,
        list 10 bugs, and check the query count is reasonable.

        Threshold is generous (< 15) — we just want to fail loudly if the
        N+1 pattern accidentally comes back for attachment counts."""
        from sqlalchemy import event
        from app.database import engine

        p = _make_project(admin_client, "PERF-2")
        for i in range(10):
            _make_bug(admin_client, p["id"], title=f"perf-{i}")

        counter = {"n": 0}

        def _on_exec(conn, cursor, statement, *args, **kwargs):
            counter["n"] += 1

        event.listen(engine, "before_cursor_execute", _on_exec)
        try:
            counter["n"] = 0
            r = admin_client.get(f"/api/bugs?project_id={p['id']}")
            assert r.status_code == 200
            assert len(r.json()["items"]) == 10
            queries = counter["n"]
        finally:
            event.remove(engine, "before_cursor_execute", _on_exec)

        # If the old N+1 pattern were back, this would be ~1 + 10 = 11+ for
        # attachment counts ALONE plus everything else, easily 20+. The new
        # path uses 1 aggregate count query for all 10 bugs.
        assert queries < 15, (
            f"list_bugs ran {queries} SQL queries for 10 bugs — N+1 regression?"
        )
