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


def main() -> int:
    db = SessionLocal()
    try:
        n = rag.index_all(db)
    finally:
        db.close()
    if n == 0:
        print("Indexed 0 documents — is SLEUTH_RAG_ENABLED set, chromadb "
              "installed, and GEMINI_API_KEY present?")
        return 1
    print(f"Indexed {n} documents into the Sleuth RAG store.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
