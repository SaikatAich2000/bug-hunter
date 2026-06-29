"""Reporting engine for Bug Hunter.

Powers both the REST API (/api/reports/*) and the Sleuth chatbot's report
intent, so the UI and the chatbot always return the same numbers.

Modules:
  catalog.py  registry of report types and their default filters
  engine.py   Filters dataclass, run_report() dispatcher, per-report queries
  xlsx.py     multi-sheet workbook writer used by both callers
"""
from __future__ import annotations

from app.reports.catalog import REPORT_CATALOG, REPORT_TYPES, get_report_meta
from app.reports.engine import (
    Filters,
    ReportColumn,
    ReportResult,
    UnknownReportError,
    run_report,
)
from app.reports.xlsx import build_workbook_bytes

__all__ = [
    "Filters",
    "ReportColumn",
    "ReportResult",
    "REPORT_CATALOG",
    "REPORT_TYPES",
    "UnknownReportError",
    "build_workbook_bytes",
    "get_report_meta",
    "run_report",
]
