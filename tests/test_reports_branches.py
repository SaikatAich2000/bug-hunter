"""Edge-case and branch tests for the Reports subsystem.

Covers branches in app/reports/engine.py, app/routes/reports.py,
app/reports/xlsx.py, and app/reports/catalog.py that the main behavior suites
(test_reports.py and test_reports_engine_helpers.py) do not reach.

Every test is hermetic and uses the temp SQLite DB the `client`/`admin_client`
fixtures provide. The few unit-level cases call engine/xlsx helpers directly.

app.* imports are placed inside test bodies rather than at module level because
the `client` fixture tears down and re-imports every app.* module between tests,
making top-level imports go stale.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from openpyxl import load_workbook

from tests.conftest import BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD


# Small API helpers, mirroring tests/test_reports.py for consistency.
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


# engine: _parse_resolution_status edge cases and run_report unknown-key error.
def test_cov_parse_resolution_status_edge_cases():
    from app.reports.engine import _parse_resolution_status
    assert _parse_resolution_status("") is None
    assert _parse_resolution_status(None) is None
    assert _parse_resolution_status("created the bug") is None
    assert _parse_resolution_status("assigned to Bob") is None
    assert _parse_resolution_status(
        "#5 'X' — status: 'New' → 'Resolved'") == "Resolved"


def test_cov_run_report_unknown_key_raises():
    from app.reports.engine import Filters, UnknownReportError, run_report
    with pytest.raises(UnknownReportError):
        run_report("definitely_not_a_report", Filters(), db=None)


def test_cov_run_route_maps_unknown_report_error_to_400(admin_client, monkeypatch):
    """The route maps UnknownReportError to 400. This path is unreachable via
    normal HTTP (catalog and _DISPATCH are kept in sync), so we force the error
    by patching run_report directly.
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


# catalog: get_report_meta — hit and miss.
def test_cov_catalog_get_report_meta():
    from app.reports.catalog import get_report_meta
    meta = get_report_meta("item_detail")            # hit
    assert meta is not None and meta["key"] == "item_detail"
    assert get_report_meta("nope_not_real") is None  # miss


# xlsx: _coerce type branches, _defang_formula_text, and the empty-summary
# early-return in _write_summary_block.
def test_cov_xlsx_coerce_branches():
    from app.reports.xlsx import _coerce, _defang_formula_text
    assert _coerce(None) == ""
    assert _coerce(True) is True                      # bool kept as-is
    assert _coerce(7) == 7
    assert _coerce(3.5) == 3.5
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert _coerce(dt) == dt.isoformat(timespec="seconds")
    assert _coerce(["a", "b"]) == "['a', 'b']"
    # Formula-leading characters are prefixed with a quote to prevent injection.
    assert _defang_formula_text("=cmd|calc") == "'=cmd|calc"
    assert _coerce("=danger") == "'=danger"


def test_cov_xlsx_build_workbook_with_empty_summary():
    """An empty summary dict exercises the early return in _write_summary_block.
    No real report produces an empty summary, so we construct one by hand."""
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
    # The summary block returned early, so no "Summary" header row should exist.
    fws = wb["Filters Applied"]
    dump = " ".join(
        str(v) for row in fws.iter_rows(values_only=True)
        for v in row if v is not None
    )
    assert "Summary" not in dump


# routes: statuses validator via /run.
def test_cov_run_statuses_filter_validator(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="new-one")  # status defaults to New
    # The validator strips unknown statuses; "New" survives, "TotallyBogusStatus" is dropped.
    body = _run(admin_client, "item_detail",
                {"statuses": ["New", "TotallyBogusStatus"]})
    titles = {row["title"] for row in body["rows"]}
    assert titles == {"new-one"}
    assert body["rows"][0]["status"] == "New"


# engine: _apply_date_range with one-sided windows (item_detail uses the shared
# helper; throughput has its own).
def test_cov_item_detail_one_sided_date_windows(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="dated")
    today = datetime.now(timezone.utc).date().isoformat()
    # date_from only.
    b_from = _run(admin_client, "item_detail", {"date_from": today})
    assert any(r["title"] == "dated" for r in b_from["rows"])
    # date_to only.
    b_to = _run(admin_client, "item_detail", {"date_to": today})
    assert any(r["title"] == "dated" for r in b_to["rows"])
    # A date_to in the far past should exclude today's row.
    b_old = _run(admin_client, "item_detail", {"date_to": "2000-01-01"})
    assert b_old["total"] == 0


# engine: resolution dedup in item_detail, seen-bug and row-None skips in
# time_to_resolution, and the non-resolution branch in timeline — all driven
# from a single twice-resolved bug.
def test_cov_twice_resolved_bug_resolution_paths(admin_client):
    p = _make_project(admin_client)
    b = _make_item(admin_client, p["id"], title="flap")
    # New -> Resolved -> Reopened -> Resolved (two resolution events).
    _change_status(admin_client, b["id"], "Resolved")
    _change_status(admin_client, b["id"], "Reopened")
    _change_status(admin_client, b["id"], "Resolved")
    # A second bug that only reaches a non-resolved state. Its status_changed
    # row exercises the row-is-None path in _ttr_row (non-resolved transition
    # for an unseen bug).
    wip = _make_item(admin_client, p["id"], title="wip-only")
    _change_status(admin_client, wip["id"], "In Progress")

    # _fetch_resolution_info sees 3 rows for the same bug; the newest
    # resolution is kept and the earlier ones hit the already-seen continue.
    detail = _run(admin_client, "item_detail", {})
    row = next(r for r in detail["rows"] if r["title"] == "flap")
    assert row["resolved_by"] == "Test Admin"
    assert row["resolved_at"]  # non-empty

    # The query yields 3 rows for that bug (ordered by created_at asc). The
    # first Resolved builds a TTR row and marks the bug seen; Reopened yields
    # None from _ttr_row; the second Resolved is skipped because the bug is
    # already seen. Exactly one TTR row results.
    ttr = _run(admin_client, "time_to_resolution", {})
    flap_rows = [r for r in ttr["rows"] if r["title"] == "flap"]
    assert len(flap_rows) == 1
    assert ttr["summary"]["count"] == 1

    # The Reopened transition is a non-resolution row (condition is False).
    # Timeline counts every resolution event without deduping by bug, so both
    # Resolved transitions are tallied.
    tl = _run(admin_client, "timeline", {})
    assert tl["summary"]["total_resolved"] == 2  # both Resolved transitions
    assert tl["summary"]["total_created"] == 2   # flap + wip-only filed today


# engine: pending_snapshot statuses intersection, empty-open-set early return,
# and by_assignee accumulation.
def test_cov_pending_snapshot_status_filter_and_assignee(admin_client):
    me = admin_client.get("/api/auth/me").json()
    p = _make_project(admin_client)
    # An open item assigned to admin so by_assignee accumulates.
    _make_item(admin_client, p["id"], title="open-assigned",
               assignee_ids=[me["id"]])

    # "New" intersects the open set, so the item appears.
    body = _run(admin_client, "pending_snapshot", {"statuses": ["New"]})
    titles = {r["title"] for r in body["rows"]}
    assert "open-assigned" in titles
    assert body["summary"]["by_assignee"].get("Test Admin", 0) >= 1

    # "Closed" never appears in the open set, so the result is the early-return
    # empty ReportResult.
    empty = _run(admin_client, "pending_snapshot", {"statuses": ["Closed"]})
    assert empty["total"] == 0
    assert empty["summary"]["total_items"] == 0


# engine: throughput with every optional filter applied at once.
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


# engine: project_breakdown — not-open, resolved, and final counter increments.
def test_cov_project_breakdown_resolved_and_final(admin_client):
    p = _make_project(admin_client, name="PBProj")
    _make_item(admin_client, p["id"], title="pb-open")       # stays New
    done = _make_item(admin_client, p["id"], title="pb-done")
    _change_status(admin_client, done["id"], "Resolved")     # hits resolved + final counters
    body = _run(admin_client, "project_breakdown", {})
    row = next(r for r in body["rows"] if r["project"] == "PBProj")
    assert row["created"] == 2
    assert row["open"] == 1        # New item
    assert row["resolved"] == 1    # Resolved item
    assert row["final"] == 1       # Resolved is a final status


# engine: aging statuses intersection and the empty-open-set path.
def test_cov_aging_status_filter_intersection_and_empty(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="aging-open")     # New (open)
    closed = _make_item(admin_client, p["id"], title="aging-closed")
    _change_status(admin_client, closed["id"], "Closed")

    # "New" intersects the open set; only the New item qualifies.
    inter = _run(admin_client, "aging", {"statuses": ["New"]})
    titles = {r["title"] for r in inter["rows"]}
    assert titles == {"aging-open"}

    # No overlap with the open set: the report returns nothing rather than
    # leaking closed items with a meaningless age.
    empty_set = _run(admin_client, "aging", {"statuses": ["Closed"]})
    assert empty_set["rows"] == []


# routes: export.xlsx with a label that sanitizes to empty, and the zebra-fill
# path triggered by two or more data rows.
def test_cov_export_xlsx_blank_label_and_zebra(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="zebra-one")
    _make_item(admin_client, p["id"], title="zebra-two")  # second row triggers zebra fill
    r = admin_client.post("/api/reports/export.xlsx", json={
        "report_key": "item_detail",
        # All chars sanitize away, leaving an empty suffix. The `if suffix:`
        # guard in _safe_filename is False, so no label is appended.
        "filters": {"label": "@@@!!!"},
    })
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    # base stays just the report key (no label suffix appended).
    assert "report-item_detail-" in cd
    wb = load_workbook(io.BytesIO(r.content), read_only=True)
    main_ws = wb[wb.sheetnames[0]]
    rows = list(main_ws.iter_rows(values_only=True))
    assert len(rows) >= 4  # banner + header + 2 data rows


# routes: 413 when row count exceeds MAX_REPORT_ROWS (patched down to 1).
def test_cov_export_xlsx_over_row_limit_returns_413(admin_client, monkeypatch):
    import app.config as config
    p = _make_project(admin_client)
    for i in range(3):
        _make_item(admin_client, p["id"], title=f"cap-{i}")
    # Patch the class attribute directly; module-level refs go stale under the
    # per-test reimport, but the route's get_settings() call picks this up.
    monkeypatch.setattr(config.Settings, "MAX_REPORT_ROWS", 1)
    r = admin_client.post("/api/reports/export.xlsx", json={
        "report_key": "item_detail",
        "filters": {},
    })
    assert r.status_code == 413, r.text
    assert "row" in r.json()["detail"].lower()


# routes: inline /run truncation when rows exceed the 1000 render cap.
# We bulk-insert via the app's DB session (1001 HTTP POSTs would be too slow).
# MAX_REPORT_ROWS is left at its default so the engine LIMIT doesn't trim below 1000.
def test_cov_run_inline_render_cap_truncates(admin_client):
    p = _make_project(admin_client, name="BulkProj")
    from app.database import SessionLocal
    from app.models import Bug
    # 1001 items so the engine returns them all (limit = MAX_REPORT_ROWS+1),
    # and the route then caps the rendered payload at 1000.
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
    # total comes from to_api() before the route trims; rows are capped at 1000.
    assert body["total"] == 1001               # full count from to_api()
    assert len(body["rows"]) == 1000           # rows capped at the render cap
    assert body["truncated"] is True
    assert body["truncated_cap"] == 1000


# routes: XlsxBuildError maps to 500. Simulate a missing openpyxl by flipping
# the module-level flag that the live build path checks.
def test_cov_export_xlsx_build_error_returns_500(admin_client, monkeypatch):
    import app.reports.xlsx as xlsx
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="boom")
    # _ensure_openpyxl() reads OPENPYXL_AVAILABLE at call time, so patching it
    # here causes build_workbook_bytes to raise XlsxBuildError -> 500.
    monkeypatch.setattr(xlsx, "OPENPYXL_AVAILABLE", False)
    r = admin_client.post("/api/reports/export.xlsx", json={
        "report_key": "item_detail",
        "filters": {},
    })
    assert r.status_code == 500
    # The error is logged server-side; the response body is intentionally generic.
    detail = r.json()["detail"].lower()
    assert "openpyxl" not in detail
    assert "workbook" in detail
