"""Shared Playwright launch settings for the browser (`ui`) suites.

PW_BROWSERS          comma list of engines, default "chromium"
PW_EXECUTABLE_PATH   use an installed browser binary instead of Playwright's download
PW_HEADED=1          show the browser window while debugging
"""
from __future__ import annotations

import os

BROWSERS = [b.strip() for b in os.environ.get("PW_BROWSERS", "chromium").split(",") if b.strip()]


def launch(playwright, name: str = "chromium"):
    options: dict = {"headless": os.environ.get("PW_HEADED", "") != "1"}
    executable = os.environ.get("PW_EXECUTABLE_PATH", "").strip()
    if executable:
        options["executable_path"] = executable
    return getattr(playwright, name).launch(**options)
