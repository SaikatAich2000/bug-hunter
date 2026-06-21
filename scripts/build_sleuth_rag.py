"""Build / refresh the Sleuth RAG index.

Usage (from the repo root, with the app's env loaded):

    SLEUTH_RAG_ENABLED=1 GEMINI_API_KEY=... python scripts/build_sleuth_rag.py

Indexes every bug, comment, and docs/ file into the local Chroma store at
SLEUTH_RAG_DIR. Safe to re-run — it upserts, so existing vectors are
replaced, not duplicated. Run it after a bulk import or on a schedule
(cron / a periodic worker). Incremental single-bug updates can call
app.chatbot.rag.upsert_bug() from the bug create/update route instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.chatbot import rag  # noqa: E402


def _misconfig_reasons() -> list[str]:
    """Reasons RAG indexing can't work at all (vs simply finding nothing)."""
    from app.config import get_settings
    s = get_settings()
    reasons: list[str] = []
    if not s.SLEUTH_RAG_ENABLED:
        reasons.append("SLEUTH_RAG_ENABLED is off")
    if not s.GEMINI_API_KEY:
        reasons.append("GEMINI_API_KEY is empty")
    try:
        import chromadb  # noqa: F401
    except ImportError:
        reasons.append("chromadb is not installed")
    return reasons


def main() -> int:
    db = SessionLocal()
    try:
        n = rag.index_all(db)
    finally:
        db.close()
    if n == 0:
        reasons = _misconfig_reasons()
        if reasons:
            # A genuine misconfiguration — fail so a cron/CI wrapper notices.
            print("Indexed 0 documents — misconfigured: " + "; ".join(reasons))
            return 1
        # Correctly configured but nothing to index (new/empty DB, no docs).
        # A no-op, not a failure: exit 0 so schedulers don't alert on an empty
        # install.
        print("Indexed 0 documents — nothing to index yet (empty corpus).")
        return 0
    print(f"Indexed {n} documents into the Sleuth RAG store.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
