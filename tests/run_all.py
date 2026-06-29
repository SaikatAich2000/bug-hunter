#!/usr/bin/env python3
"""Run every Sleuth test file in sequence.

Usage:  python3 tests/run_all.py
or:     ./tests/run_all.py

Exits non-zero if any file fails. Each test file lives in a fresh temp
SQLite database and never touches the production data — see
test_sleuth_safety.py for the formal guarantees.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Ordering doesn't affect correctness; each file is hermetic.
TEST_FILES = [
    "test_sleuth_parser.py",
    "test_sleuth_actions.py",
    "test_sleuth_classifier.py",
    "test_sleuth_safety.py",
    "test_sleuth_comprehensive.py",
]


def main() -> int:
    overall_ok = True
    totals = {"passed": 0, "failed": 0}
    print("=" * 64)
    print(" Sleuth — full test suite")
    print("=" * 64)
    for fname in TEST_FILES:
        path = HERE / fname
        if not path.exists():
            print(f"[skip] {fname} (not found)")
            continue
        print(f"\n>>> {fname}")
        # Run as subprocess so each file's env-var setup is fresh.
        res = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            capture_output=True, text=True,
        )
        ok = (res.returncode == 0)
        out_lines = res.stdout.strip().splitlines()
        # Print the tail so the user sees each file's summary block.
        for line in out_lines[-6:]:
            print("    " + line)
        if res.stderr.strip():
            print("    [stderr]")
            for line in res.stderr.strip().splitlines()[-10:]:
                print("    " + line)
        for line in out_lines:
            if line.startswith("Passed:"):
                try:
                    totals["passed"] += int(line.split(":", 1)[1].strip())
                except Exception:
                    pass
            elif line.startswith("Failed:"):
                try:
                    totals["failed"] += int(line.split(":", 1)[1].strip())
                except Exception:
                    pass
        if not ok:
            overall_ok = False
            print(f"    [{fname} FAILED]")

    print("\n" + "=" * 64)
    print(f" Overall: {totals['passed']} passed, {totals['failed']} failed")
    print("=" * 64)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
