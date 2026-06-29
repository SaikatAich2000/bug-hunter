"""Retrieval-Augmented Generation for Sleuth's cloud layer.

Indexes bugs, comments, and any plain-text/markdown docs under
SLEUTH_DOCS_DIR into a local Chroma vector store using Gemini embeddings.
retrieve_text() returns the top-k snippets as a compact context block for
cloud_llm.

This deployment is single-tenant, so retrieval is not org-filtered. The
`where` parameter on the Chroma query is the seam for a future tenant/project
filter; mirror the SQL handlers' scoping there if this ever becomes
multi-tenant.

Everything is lazy and gated: with SLEUTH_RAG_ENABLED off, chromadb missing,
or no embedding key, retrieve_text() returns "" and the cloud layer runs
without grounding. Nothing here can break startup.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Bug, Comment

logger = logging.getLogger("bug_hunter.sleuth.rag")

_COLLECTION = "sleuth"


def _embed(texts: list[str]) -> Optional[list[list[float]]]:
    """Batch-embed texts with Gemini. Returns None on any failure."""
    s = get_settings()
    if not s.GEMINI_API_KEY or not texts:
        return None
    try:
        import httpx
        from app.chatbot.redaction import redact
        model = f"models/{s.GEMINI_EMBED_MODEL}"
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/{model}:batchEmbedContents",
            # Key in a header rather than the URL query string so it won't
            # appear in logged exception URLs or proxy access logs (same
            # approach as cloud_llm.py).
            headers={"x-goog-api-key": s.GEMINI_API_KEY},
            # Redact before sending: bug descriptions, comment bodies, and
            # doc content are outbound egress just like the generation path,
            # so they pass the same PII gate. Redact first, then truncate.
            json={"requests": [
                {"model": model, "content": {"parts": [{"text": redact(t)[:8000]}]}}
                for t in texts
            ]},
            timeout=s.SLEUTH_CLOUD_TIMEOUT_S,
        )
        r.raise_for_status()
        return [e["values"] for e in r.json()["embeddings"]]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sleuth RAG embedding failed: %s", exc)
        return None


def _collection():
    """Return the Chroma collection, or None if unavailable."""
    s = get_settings()
    if not s.SLEUTH_RAG_ENABLED:
        return None
    try:
        import chromadb
        client = chromadb.PersistentClient(path=s.SLEUTH_RAG_DIR)
        # Embeddings are supplied explicitly, so no embedding_function needed.
        return client.get_or_create_collection(
            _COLLECTION, metadata={"hnsw:space": "cosine"}
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sleuth RAG store unavailable (chromadb?): %s", exc)
        return None


def _doc_text(bug: Bug) -> str:
    parts = [f"#{bug.id} [{bug.item_type}] {bug.title}",
             f"status={bug.status} priority={bug.priority} env={bug.environment}"]
    if bug.description:
        parts.append(bug.description)
    return "\n".join(parts)


def _gather_db_docs(db: Session) -> tuple[list[str], list[str], list[dict]]:
    """Collect (ids, docs, metas) for every bug + comment in the DB."""
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    for bug in db.scalars(select(Bug)).all():
        ids.append(f"bug:{bug.id}")
        docs.append(_doc_text(bug))
        metas.append({"kind": "bug", "bug_id": bug.id, "title": bug.title})
    for c in db.scalars(select(Comment)).all():
        ids.append(f"comment:{c.id}")
        docs.append(f"Comment on #{c.bug_id} by {c.author_name}: {c.body}")
        metas.append({"kind": "comment", "bug_id": c.bug_id})
    return ids, docs, metas


def _read_doc(path: str) -> Optional[str]:
    """Read a text/markdown doc (capped), or None if it can't be read."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read()[:8000]
    except OSError:
        return None


def _gather_file_docs(docs_dir: str) -> tuple[list[str], list[str], list[dict]]:
    """Collect (ids, docs, metas) for every .md/.txt file under docs_dir."""
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    if not os.path.isdir(docs_dir):
        return ids, docs, metas
    for root, _dirs, files in os.walk(docs_dir):
        for fn in files:
            if not fn.lower().endswith((".md", ".txt")):
                continue
            path = os.path.join(root, fn)
            body = _read_doc(path)
            if body is None:
                continue
            ids.append(f"doc:{os.path.relpath(path, docs_dir)}")
            docs.append(f"Doc {fn}:\n{body}")
            metas.append({"kind": "doc", "path": fn})
    return ids, docs, metas


def _embed_upsert(col, ids: list[str], docs: list[str], metas: list[dict]) -> int:
    """Embed + upsert in 64-doc batches; stop early if embedding fails."""
    written = 0
    for i in range(0, len(docs), 64):
        chunk = docs[i:i + 64]
        vecs = _embed(chunk)
        if vecs is None:
            break
        col.upsert(ids=ids[i:i + 64], documents=chunk,
                   embeddings=vecs, metadatas=metas[i:i + 64])
        written += len(chunk)
    return written


def index_all(db: Session) -> int:
    """Full (re)index of bugs, comments, and docs. Returns the doc count.
    Called from scripts/build_sleuth_rag.py or a periodic job."""
    col = _collection()
    if col is None:
        return 0
    ids, docs, metas = _gather_db_docs(db)
    f_ids, f_docs, f_metas = _gather_file_docs(get_settings().SLEUTH_DOCS_DIR)
    ids += f_ids
    docs += f_docs
    metas += f_metas
    if not docs:
        return 0
    written = _embed_upsert(col, ids, docs, metas)
    logger.info("Sleuth RAG indexed %d documents", written)
    return written


def upsert_bug(db: Session, bug_id: int) -> None:
    """Incrementally (re)index a single bug. Best-effort; never raises."""
    try:
        col = _collection()
        if col is None:
            return
        bug = db.get(Bug, bug_id)
        if bug is None:
            return
        vecs = _embed([_doc_text(bug)])
        if vecs:
            col.upsert(ids=[f"bug:{bug.id}"], documents=[_doc_text(bug)],
                       embeddings=vecs,
                       metadatas=[{"kind": "bug", "bug_id": bug.id}])
    except Exception:  # noqa: BLE001
        logger.debug("Sleuth RAG upsert_bug failed", exc_info=True)


def retrieve_text(message: str, where: Optional[dict] = None) -> str:
    """Return a compact context block of the top-k snippets, or "" if RAG
    is disabled or unavailable.

    `where` is the multi-tenant scoping seam: pass a Chroma metadata filter
    here to restrict retrieval to a specific tenant/project. Unused in this
    single-tenant build, so the caller need not pass user/session info."""
    col = _collection()
    if col is None:
        return ""
    qvec = _embed([message])
    if not qvec:
        return ""
    try:
        s = get_settings()
        res = col.query(query_embeddings=qvec, n_results=s.SLEUTH_RAG_TOP_K,
                        where=where)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sleuth RAG query failed: %s", exc)
        return ""
    docs = (res.get("documents") or [[]])[0]
    if not docs:
        return ""
    # Wrap in a fenced data block and defang any literal fence marker so
    # indexed content (arbitrary comment/doc bodies) can't smuggle instructions
    # to the model. Same structural injection defense as retrieval.format_context.
    body = "\n---\n".join(d.replace("<<", "< <") for d in docs)
    return f"<<DATA>>\n{body}\n<<END DATA>>"


__all__ = ["retrieve_text", "index_all", "upsert_bug"]
