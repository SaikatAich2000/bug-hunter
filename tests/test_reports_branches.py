"""Edge-case and branch tests for the Reports subsystem.

Covers behavior in:
  * app/reports/engine.py
  * app/routes/reports.py
  * app/reports/xlsx.py
  * app/reports/catalog.py

that the broader behavior suite (tests/test_reports.py and
tests/test_reports_engine_helpers.py) does not. Every test is hermetic: it
talks to the temp SQLite DB the `client`/`admin_client` fixtures spin up, and
the few cases that need a non-DB unit call the engine/xlsx helpers directly.

Some app.* modules are imported inside the test bodies because the `client`
fixture deletes and re-imports every app.* module per test, so a module
captured at this file's top level goes stale. Importing inside the test, and
for config patching the config.Settings class attribute, pins the exact
generation the running app reads.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from openpyxl import load_workbook

from tests.conftest import BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD


# ---------------------------------------------------------------------------
# Small API helpers (mirror tests/test_reports.py so behaviour stays aligned)
# ---------------------------------------------------------------------------
def _make_project(client, name="CovProj"):
    r = client.post("/api/projects", json={"name": name, "color": "#123456"})
    assert r.status_code == 201, r.text
    return r.json()


def _make_item(client, project_id, **extra):
    body = {
        "title": "cov item",
        "project_id": project_id,
        "priority": "High",
        "environment": "DEV",
        "item_type": "Bug",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _change_status(client, bug_id, new_status):
    r = client.put(f"/api/bugs/{bug_id}", json={"status": new_status})
    assert r.status_code == 200, r.text
    return r.json()


def _run(client, report_key, filters=None):
    r = client.post("/api/reports/run", json={
        "report_key": report_key,
        "filters": filters or {},
    })
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# engine: _parse_resolution_status with empty and non-matching detail;
# run_report raises on an unknown key (direct unit calls).
# ---------------------------------------------------------------------------
def test_cov_parse_resolution_status_edge_cases():
    from app.reports.engine import _parse_resolution_status
    # Falsy detail -> None.
    assert _parse_resolution_status("") is None
    assert _parse_resolution_status(None) is None
    # Present but non-matching detail -> None.
    assert _parse_resolution_status("created the bug") is None
    assert _parse_resolution_status("assigned to Bob") is None
    # A real status line still parses.
    assert _parse_resolution_status(
        "#5 'X' — status: 'New' → 'Resolved'") == "Resolved"


def test_cov_run_report_unknown_key_raises():
    from app.reports.engine import Filters, UnknownReportError, run_report
    with pytest.raises(UnknownReportError):
        run_report("definitely_not_a_report", Filters(), db=None)


def test_cov_run_route_maps_unknown_report_error_to_400(admin_client, monkeypatch):
    """When the catalog gate passes for a valid key but run_report itself
    raises UnknownReportError, the route turns it into a 400. This is
    unreachable through pure HTTP (catalog and _DISPATCH are kept in sync),
    so the test forces run_report to raise.
    """
    import app.routes.reports as reports_route
    from app.reports.engine import UnknownReportError

    def _boom(_key, _filters, _db):
        raise UnknownReportError("forced unknown")

    monkeypatch.setattr(reports_route, "run_report", _boom)
    r = admin_client.post("/api/reports/run", json={
        "report_key": "item_detail",   # valid, so it passes the catalog check
        "filters": {},
    })
    assert r.status_code == 400
    assert "forced unknown" in r.json()["detail"]


# ---------------------------------------------------------------------------
# catalog: get_report_meta hit and miss
# ---------------------------------------------------------------------------
def test_cov_catalog_get_report_meta():
    from app.reports.catalog import get_report_meta
    meta = get_report_meta("item_detail")            # hit
    assert meta is not None and meta["key"] == "item_detail"
    assert get_report_meta("nope_not_real") is None  # miss


# ---------------------------------------------------------------------------
# xlsx: _coerce type branches, _defang_formula_text, and the empty-summary
# early return in _write_summary_block (via the full pipeline).
# ---------------------------------------------------------------------------
def test_cov_xlsx_coerce_branches():
    from app.reports.xlsx import _coerce, _defang_formula_text
    assert _coerce(None) == ""
    assert _coerce(True) is True                      # bool kept as-is
    assert _coerce(7) == 7
    assert _coerce(3.5) == 3.5
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert _coerce(dt) == dt.isoformat(timespec="seconds")
    # Other types are stringified and defanged.
    assert _coerce(["a", "b"]) == "['a', 'b']"
    # Formula-leading text gets quoted.
    assert _defang_formula_text("=cmd|calc") == "'=cmd|calc"
    assert _coerce("=danger") == "'=danger"


def test_cov_xlsx_build_workbook_with_empty_summary():
    """A ReportResult whose summary is empty exercises the early return in
    _write_summary_block. No report produces an empty summary in practice, so
    the result is built by hand and run through the real writer."""
    from app.reports.engine import ReportColumn, ReportResult
    from app.reports.xlsx import build_workbook_bytes
    result = ReportResult(
        report_key="custom",
        report_label="Custom Empty Report",   # avoid the word "Summary" here
        columns=[ReportColumn("id", "ID", 8, kind="number"),
                 ReportColumn("title", "Title", 30)],
        rows=[{"id": 1, "title": "one"}, {"id": 2, "title": "two"}],
        summary={},                       # empty -> early return
        filters={},
    )
    data = build_workbook_bytes(result)
    wb = load_workbook(io.BytesIO(data), read_only=True)
    assert "Filters Applied" in wb.sheetnames
    # No "Summary" header row should appear because the block returned early.
    fws = wb["Filters Applied"]
    dump = " ".join(
        str(v) for row in fws.iter_rows(values_only=True)
        for v in row if v is not None
    )
    assert "Summary" not in dump


# ---------------------------------------------------------------------------
# routes: statuses validator via /run
# ---------------------------------------------------------------------------
def test_cov_run_statuses_filter_validator(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="new-one")  # status defaults to New
    # Mix a valid status with a bogus one; the validator keeps only the
    # allowed one, and the status filter then applies.
    body = _run(admin_client, "item_detail",
                {"statuses": ["New", "TotallyBogusStatus"]})
    titles = {row["title"] for row in body["rows"]}
    assert titles == {"new-one"}
    assert body["rows"][0]["status"] == "New"


# ---------------------------------------------------------------------------
# engine: _apply_date_range one-sided windows. item_detail routes through
# _apply_date_range (throughput uses its own).
# ---------------------------------------------------------------------------
def test_cov_item_detail_one_sided_date_windows(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="dated")
    today = datetime.now(timezone.utc).date().isoformat()
    # date_from only (date_to branch skipped).
    b_from = _run(admin_client, "item_detail", {"date_from": today})
    assert any(r["title"] == "dated" for r in b_from["rows"])
    # date_to only (date_from branch skipped).
    b_to = _run(admin_client, "item_detail", {"date_to": today})
    assert any(r["title"] == "dated" for r in b_to["rows"])
    # A date_to in the far past excludes today's row (proves the bound binds).
    b_old = _run(admin_client, "item_detail", {"date_to": "2000-01-01"})
    assert b_old["total"] == 0


# ---------------------------------------------------------------------------
# engine: resolution-info dedup via item_detail, the time_to_resolution
# seen-bug and row-None skips, and the timeline non-resolution branch, all
# from one twice-resolved bug.
# ---------------------------------------------------------------------------
def test_cov_twice_resolved_bug_resolution_paths(admin_client):
    p = _make_project(admin_client)
    b = _make_item(admin_client, p["id"], title="flap")
    # New -> Resolved (resolution #1) -> Reopened (non-res) -> Resolved (#2).
    _change_status(admin_client, b["id"], "Resolved")
    _change_status(admin_client, b["id"], "Reopened")
    _change_status(admin_client, b["id"], "Resolved")
    # A second bug that only ever goes to a non-resolved state: its single
    # status_changed row reaches _ttr_row for an as-yet-unseen bug, which
    # returns None, exercising the row-is-None skip.
    wip = _make_item(admin_client, p["id"], title="wip-only")
    _change_status(admin_client, wip["id"], "In Progress")

    # item_detail: _fetch_resolution_info sees 3 status_changed rows for the
    # same bug. The newest resolution is captured and the rest hit the
    # already-seen continue. The row must still surface with a resolver.
    detail = _run(admin_client, "item_detail", {})
    row = next(r for r in detail["rows"] if r["title"] == "flap")
    assert row["resolved_by"] == "Test Admin"
    assert row["resolved_at"]  # non-empty

    # time_to_resolution: for that bug the query yields 3 rows ordered by
    # created_at asc. The first Resolved builds a row and marks the bug seen;
    # Reopened yields None from _ttr_row; the second Resolved is skipped
    # because the bug_id is already seen. Exactly one TTR row results.
    ttr = _run(admin_client, "time_to_resolution", {})
    flap_rows = [r for r in ttr["rows"] if r["title"] == "flap"]
    assert len(flap_rows) == 1
    assert ttr["summary"]["count"] == 1

    # timeline: the Reopened status change is a non-resolution row, so the
    # resolved-bucketing condition is False for it. The two Resolved
    # transitions both count (timeline tallies every resolution event and does
    # not dedup by bug), so total_resolved == 2.
    tl = _run(admin_client, "timeline", {})
    assert tl["summary"]["total_resolved"] == 2  # both Resolved transitions
    assert tl["summary"]["total_created"] == 2   # flap + wip-only filed today


# ---------------------------------------------------------------------------
# engine: pending_snapshot statuses-intersection, the empty early return, and
# by_assignee accumulation.
# ---------------------------------------------------------------------------
def test_cov_pending_snapshot_status_filter_and_assignee(admin_client):
    me = admin_client.get("/api/auth/me").json()
    p = _make_project(admin_client)
    # An open item assigned to admin so by_assignee accumulates.
    _make_item(admin_client, p["id"], title="open-assigned",
               assignee_ids=[me["id"]])

    # A statuses filter that intersects the open set keeps "New".
    body = _run(admin_client, "pending_snapshot", {"statuses": ["New"]})
    titles = {r["title"] for r in body["rows"]}
    assert "open-assigned" in titles
    assert body["summary"]["by_assignee"].get("Test Admin", 0) >= 1

    # A statuses filter that does not intersect the open set yields an empty
    # open set and an early-return ReportResult. "Closed" is never open.
    empty = _run(admin_client, "pending_snapshot", {"statuses": ["Closed"]})
    assert empty["total"] == 0
    assert empty["summary"]["total_items"] == 0


# ---------------------------------------------------------------------------
# engine: throughput query filter branches, exercising every bug-side filter
# at once.
# ---------------------------------------------------------------------------
def test_cov_throughput_all_query_filters(admin_client):
    me = admin_client.get("/api/auth/me").json()
    p = _make_project(admin_client, name="ThruProj")
    ev = admin_client.post("/api/events", json={"name": "ThruEvent"}).json()
    target = _make_item(
        admin_client, p["id"], title="thru-target",
        priority="Critical", environment="PROD",
        assignee_ids=[me["id"]], event_id=ev["id"],
    )
    _change_status(admin_client, target["id"], "Resolved")
    body = _run(admin_client, "throughput", {
        "project_ids": [p["id"]],
        "event_id": ev["id"],
        "assignee_ids": [me["id"]],
        "reporter_ids": [me["id"]],
        "priorities": ["Critical"],
        "environments": ["PROD"],
    })
    assert body["summary"]["total_resolved"] == 1
    admin_row = next(r for r in body["rows"] if r["user_name"] == "Test Admin")
    assert admin_row["resolved"] == 1
    assert admin_row["bugs"] == 1


# ---------------------------------------------------------------------------
# engine: project_breakdown not-open branch plus resolved and final
# increments, which need a resolved Bug in the set.
# ---------------------------------------------------------------------------
def test_cov_project_breakdown_resolved_and_final(admin_client):
    p = _make_project(admin_client, name="PBProj")
    _make_item(admin_client, p["id"], title="pb-open")       # stays New (open)
    done = _make_item(admin_client, p["id"], title="pb-done")
    _change_status(admin_client, done["id"], "Resolved")     # resolved + final
    body = _run(admin_client, "project_breakdown", {})
    row = next(r for r in body["rows"] if r["project"] == "PBProj")
    assert row["created"] == 2
    assert row["open"] == 1        # the New item
    assert row["resolved"] == 1    # the Resolved item
    assert row["final"] == 1       # Resolved is a final status


# ---------------------------------------------------------------------------
# engine: aging statuses-intersection and the empty-open-set path that skips
# the status WHERE clause.
# ---------------------------------------------------------------------------
def test_cov_aging_status_filter_intersection_and_empty(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="aging-open")     # New (open)
    closed = _make_item(admin_client, p["id"], title="aging-closed")
    _change_status(admin_client, closed["id"], "Closed")

    # statuses intersects the open set: only the New item qualifies.
    inter = _run(admin_client, "aging", {"statuses": ["New"]})
    titles = {r["title"] for r in inter["rows"]}
    assert titles == {"aging-open"}

    # statuses with NO overlap with the open set -> an "open items" report has
    # nothing to show (mirrors pending_snapshot), rather than silently leaking
    # closed items stamped with a bogus age.
    empty_set = _run(admin_client, "aging", {"statuses": ["Closed"]})
    assert empty_set["rows"] == []


# ---------------------------------------------------------------------------
# routes: export.xlsx label that sanitizes to empty, and the zebra-fill path
# with two or more main-sheet rows.
# ---------------------------------------------------------------------------
def test_cov_export_xlsx_blank_label_and_zebra(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="zebra-one")
    _make_item(admin_client, p["id"], title="zebra-two")  # second row triggers zebra fill
    r = admin_client.post("/api/reports/export.xlsx", json={
        "report_key": "item_detail",
        # label is all formula/non-safe chars, sanitizing to "", so the
        # `if suffix:` guard in _safe_filename is False.
        "filters": {"label": "@@@!!!"},
    })
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    # base stays just the report key (no label suffix appended).
    assert "report-item_detail-" in cd
    wb = load_workbook(io.BytesIO(r.content), read_only=True)
    main_ws = wb[wb.sheetnames[0]]
    rows = list(main_ws.iter_rows(values_only=True))
    # banner + header + 2 data rows.
    assert len(rows) >= 4


# ---------------------------------------------------------------------------
# routes: 413 when the row count exceeds MAX_REPORT_ROWS. Seed a handful of
# rows, then patch the class attribute down to 1.
# ---------------------------------------------------------------------------
def test_cov_export_xlsx_over_row_limit_returns_413(admin_client, monkeypatch):
    import app.config as config
    p = _make_project(admin_client)
    for i in range(3):
        _make_item(admin_client, p["id"], title=f"cap-{i}")
    # Patch the Settings class attribute so the freshly-built get_settings()
    # instance the route reads sees the tiny cap (module-level refs are stale
    # under the per-test reimport).
    monkeypatch.setattr(config.Settings, "MAX_REPORT_ROWS", 1)
    r = admin_client.post("/api/reports/export.xlsx", json={
        "report_key": "item_detail",
        "filters": {},
    })
    assert r.status_code == 413, r.text
    assert "row" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# routes: inline /run truncation when rows exceed the 1000 render cap.
# Bulk-insert via the app's own session so the test stays fast (1001 HTTP
# POSTs would be needlessly slow) and hermetic. MAX_REPORT_ROWS keeps its
# default so the engine LIMIT doesn't trim below 1000.
# ---------------------------------------------------------------------------
def test_cov_run_inline_render_cap_truncates(admin_client):
    p = _make_project(admin_client, name="BulkProj")
    from app.database import SessionLocal
    from app.models import Bug
    # 1001 items: the engine returns them all (limit = MAX_REPORT_ROWS+1), and
    # the route caps the rendered payload at 1000 and flags it truncated.
    db = SessionLocal()
    try:
        db.add_all([
            Bug(project_id=p["id"], title=f"bulk-{i}", description="",
                item_type="Bug", status="New", priority="Low",
                environment="DEV")
            for i in range(1001)
        ])
        db.commit()
    finally:
        db.close()
    body = _run(admin_client, "item_detail", {})
    # `total` reflects the full result (computed before the route trims); the
    # rendered `rows` list is capped at 1000 and flagged truncated.
    assert body["total"] == 1001               # full count from to_api()
    assert len(body["rows"]) == 1000           # rows capped at the render cap
    assert body["truncated"] is True
    assert body["truncated_cap"] == 1000


# ---------------------------------------------------------------------------
# routes: XlsxBuildError maps to 500, and xlsx _ensure_openpyxl raises.
# Simulate a server with openpyxl unavailable by flipping the module flag the
# live build path reads.
# ---------------------------------------------------------------------------
def test_cov_export_xlsx_build_error_returns_500(admin_client, monkeypatch):
    import app.reports.xlsx as xlsx
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="boom")
    # build_workbook_bytes calls _ensure_openpyxl(), which reads
    # OPENPYXL_AVAILABLE live; forcing it False raises XlsxBuildError, which
    # the route maps to a 500.
    monkeypatch.setattr(xlsx, "OPENPYXL_AVAILABLE", False)
    r = admin_client.post("/api/reports/export.xlsx", json={
        "report_key": "item_detail",
        "filters": {},
    })
    assert r.status_code == 500
    # The 500 body is generic; the dependency-state cause is logged
    # server-side, not echoed to the client.
    detail = r.json()["detail"].lower()
    assert "openpyxl" not in detail
    assert "workbook" in detail
