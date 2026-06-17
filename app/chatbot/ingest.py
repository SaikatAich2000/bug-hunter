"""Sleuth document-ingest — the admin "upload a doc full of bugs" feature.

This is Sleuth's one deliberate WRITE surface, and it is **admin-only** (the
route in router.py enforces it). An admin uploads a document listing bugs and
Sleuth turns each entry into a real work item, with the same validation +
audit trail as the REST create endpoint.

Two extraction paths, in order of preference:

  1. AI — when the cloud assistant is configured (SLEUTH_CLOUD_ENABLED + a key),
     the raw text is handed to the model, which returns a structured list of
     bug specs. This is the "Sleuth AI analyzes the document" path.

  2. Deterministic parser — always available, no network. Understands xlsx,
     JSON, CSV and free-form text / markdown (one bug per line / bullet /
     numbered item, with optional `[Priority]` tags and `Title | Description`).

Both paths converge on the same cleaned `spec` shape, so the rest of the
pipeline (validation, creation, audit) is identical regardless of how the
document was read. The AI is an enhancement layered on top of a parser that
fully stands on its own — so the feature works on a box with no API key, and
the test-suite covers it without ever making a network call.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Activity, Bug, Project, User
from app.schemas import (
    ALLOWED_ENVIRONMENTS, ALLOWED_ITEM_TYPES, ALLOWED_PRIORITIES,
    MIN_TITLE_LENGTH, sanitize_html,
)

logger = logging.getLogger("bug_hunter.sleuth.ingest")

# Hard cap on how many items one upload can create — protects the DB from a
# runaway 100k-row spreadsheet and keeps the request bounded.
MAX_SPECS = 500
# Soft cap on the uploaded document size (bytes) — the route also enforces the
# global body limit, but we bound the decode here too.
MAX_DOC_BYTES = 5 * 1024 * 1024  # 5 MB of text/spreadsheet is plenty


# ---------------------------------------------------------------------------
# Synonym maps — make the parser forgiving of however the admin wrote the doc.
# ---------------------------------------------------------------------------
_PRIORITY_SYNONYMS = {
    "critical": "Critical", "blocker": "Critical", "p0": "Critical", "urgent": "Critical",
    "high": "High", "p1": "High", "major": "High",
    "medium": "Medium", "p2": "Medium", "normal": "Medium", "med": "Medium",
    "low": "Low", "p3": "Low", "minor": "Low", "trivial": "Low",
}
_TYPE_SYNONYMS = {
    "bug": "Bug", "defect": "Bug", "issue": "Bug",
    "requirement": "Requirement", "story": "Requirement", "feature": "Requirement",
    "task": "Task", "todo": "Task", "chore": "Task",
}
_ENV_SYNONYMS = {
    "prod": "PROD", "production": "PROD", "live": "PROD",
    "uat": "UAT", "staging": "UAT", "stage": "UAT", "qa": "UAT", "test": "UAT",
    "dev": "DEV", "development": "DEV", "local": "DEV",
}

# Field-name aliases for structured rows (CSV header / JSON keys / xlsx header).
_TITLE_KEYS = ("title", "summary", "name", "bug", "issue", "task", "item")
_DESC_KEYS = ("description", "desc", "details", "detail", "notes", "note", "body")
_PRIORITY_KEYS = ("priority", "prio", "severity", "sev")
_TYPE_KEYS = ("type", "item_type", "itemtype", "category", "kind")
_ENV_KEYS = ("environment", "env")


def _norm_from(value: Any, synonyms: dict[str, str], allowed: list[str], default: str) -> str:
    """Resolve a free-text value to a canonical enum: exact (case-insensitive)
    match wins, then the synonym table, else the default."""
    if value is None:
        return default
    s = str(value).strip().lower()
    if not s:
        return default
    for canonical in allowed:
        if canonical.lower() == s:
            return canonical
    return synonyms.get(s, default)


def _first_key(row: dict, keys: tuple[str, ...]) -> Any:
    """First present, non-empty value among `keys` for a dict whose own keys may
    be any case AND may be multi-word ("Bug Summary", "Sev."). We match exactly
    first, then fall back to a word-level contains check (in `keys` priority
    order) so real-world column headers map to the right field."""
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for k in keys:
        if k in low and low[k] not in (None, ""):
            return low[k]
    # Fuzzy, in priority order: a column whose name CONTAINS the key as a word
    # (e.g. "bug summary" → summary → title; "sev" → priority).
    for k in keys:
        for col, v in low.items():
            if v in (None, ""):
                continue
            if k in re.split(r"[^a-z0-9]+", col):
                return v
    return None


def _clean_spec(raw: Any) -> Optional[dict]:
    """Normalise one raw entry (dict or bare string) into a validated spec, or
    None if it has no usable title."""
    if isinstance(raw, str):
        raw = {"title": raw}
    if not isinstance(raw, dict):
        return None
    title = _first_key(raw, _TITLE_KEYS)
    title = str(title).strip() if title is not None else ""
    if len(title) < MIN_TITLE_LENGTH:
        return None
    desc = _first_key(raw, _DESC_KEYS)
    return {
        "title": title[:200],
        "description": str(desc).strip() if desc is not None else "",
        "priority": _norm_from(_first_key(raw, _PRIORITY_KEYS), _PRIORITY_SYNONYMS, ALLOWED_PRIORITIES, "Medium"),
        "item_type": _norm_from(_first_key(raw, _TYPE_KEYS), _TYPE_SYNONYMS, ALLOWED_ITEM_TYPES, "Bug"),
        "environment": _norm_from(_first_key(raw, _ENV_KEYS), _ENV_SYNONYMS, ALLOWED_ENVIRONMENTS, "DEV"),
    }


def _clean_all(rows: list[Any]) -> list[dict]:
    out: list[dict] = []
    for raw in rows:
        spec = _clean_spec(raw)
        if spec is not None:
            out.append(spec)
        if len(out) >= MAX_SPECS:
            break
    return out


# ---------------------------------------------------------------------------
# Format detection + per-format parsers (deterministic path)
# ---------------------------------------------------------------------------
def _specs_from_json(text: str) -> Optional[list[dict]]:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        for key in ("bugs", "items", "issues", "tasks", "rows", "data"):
            if isinstance(data.get(key), list):
                return _clean_all(data[key])
        # A single object describing one bug.
        return _clean_all([data])
    if isinstance(data, list):
        return _clean_all(data)
    return None


def _specs_from_csv(text: str) -> list[dict]:
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    return _rows_to_specs(rows)


# Leading list markers (bullets, numbers, checkboxes, blockquotes, headings).
_LINE_MARKER = re.compile(r"^\s*(?:[-*+>]\s+|\d+[.)]\s+|\[[ xX]\]\s+|#{1,6}\s+)")
# A line that's just a separator / rule.
_SEPARATOR = re.compile(r"^[\s\-=*_#.]+$")
# A trailing/leading priority tag like [High] or (Critical).
_PRIORITY_TAG = re.compile(r"[\[(]\s*(critical|blocker|p0|urgent|high|p1|major|"
                           r"medium|p2|normal|low|p3|minor|trivial)\s*[\])]", re.IGNORECASE)


def _split_line_meta(line: str) -> dict:
    """Turn one free-text line into a raw spec dict, pulling out a `[Priority]`
    tag and a `Title | Description` split."""
    raw: dict[str, Any] = {}
    tag = _PRIORITY_TAG.search(line)
    if tag:
        raw["priority"] = tag.group(1)
        line = _PRIORITY_TAG.sub("", line).strip()
    if "|" in line:
        title, _, desc = line.partition("|")
        raw["title"] = title.strip()
        raw["description"] = desc.strip()
    else:
        raw["title"] = line.strip()
    return raw


def _specs_from_lines(text: str) -> list[dict]:
    raws: list[dict] = []
    for line in text.splitlines():
        stripped = _LINE_MARKER.sub("", line).strip()
        if not stripped or _SEPARATOR.match(stripped):
            continue
        raws.append(_split_line_meta(stripped))
    return _clean_all(raws)


# Words that, when seen in a first row, mark it as a header rather than data.
_HEADER_WORDS = frozenset(
    _TITLE_KEYS + _DESC_KEYS + _PRIORITY_KEYS + _TYPE_KEYS + _ENV_KEYS
    + ("status", "state", "assignee", "owner", "id", "#", "no", "no.")
)


def _looks_like_header(row: list[str]) -> bool:
    """True if the row names a recognized column (title / priority / status /
    …) — the reliable signal that it's a header, not data."""
    return any(c.strip().lower() in _HEADER_WORDS for c in row if c.strip())


def _xlsx_rows(raw: bytes) -> Optional[list[list[str]]]:
    """Read the first sheet's non-empty rows as lists of strings, or None if the
    bytes aren't a readable workbook (so the caller falls through to text)."""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 — not an xlsx / corrupt → fall through
        return None
    try:
        ws = wb.active
        rows = [
            [("" if c is None else str(c)) for c in row]
            for row in ws.iter_rows(values_only=True)
        ]
    finally:
        wb.close()
    return [r for r in rows if any(c.strip() for c in r)]


def _rows_to_specs(rows: list[list[str]]) -> list[dict]:
    """Turn a table's rows into specs: if the first row is a header, map columns
    by name; otherwise (esp. a 1-column list) treat each first cell as a title.
    A 2+-column sheet whose first row isn't recognisably data is treated as
    headered — so a header row never becomes a bogus 'bug'."""
    if not rows:
        return []
    multi_col = max((len(r) for r in rows), default=1) >= 2
    if _looks_like_header(rows[0]) or multi_col:
        header = [c.strip().lower() for c in rows[0]]
        return _clean_all([dict(zip(header, r)) for r in rows[1:]])
    return _clean_all([r[0] for r in rows])


def _specs_from_xlsx(raw: bytes) -> Optional[list[dict]]:
    """Parse the first sheet of an xlsx workbook into specs (deterministic
    fallback when the AI layer is off). Returns None if the bytes aren't a
    readable workbook."""
    rows = _xlsx_rows(raw)
    if rows is None:
        return None
    return _rows_to_specs(rows)


def _looks_like_xlsx(filename: str, raw: bytes) -> bool:
    # xlsx is a zip — magic bytes "PK\x03\x04". Combine with the extension so a
    # plain zip of something else doesn't get mis-parsed silently.
    return filename.lower().endswith(".xlsx") or raw[:4] == b"PK\x03\x04"


def parse_document(filename: str, raw: bytes) -> list[dict]:
    """Deterministic parse of an uploaded document into cleaned bug specs.
    Tries the format the bytes/extension suggest, then falls back to line
    parsing so a misnamed text file still yields items."""
    filename = filename or ""
    if _looks_like_xlsx(filename, raw):
        specs = _specs_from_xlsx(raw)
        if specs is not None:
            return specs
    # Decode text once for the remaining formats.
    text = raw.decode("utf-8", errors="replace")
    stripped = text.lstrip()
    if filename.lower().endswith(".json") or stripped[:1] in ("[", "{"):
        specs = _specs_from_json(text)
        if specs is not None:
            return specs
    if filename.lower().endswith(".csv") or ("," in (text.splitlines() or [""])[0]):
        csv_specs = _specs_from_csv(text)
        if csv_specs:
            return csv_specs
    return _specs_from_lines(text)


# ---------------------------------------------------------------------------
# AI extraction (optional, isolated, mockable)
# ---------------------------------------------------------------------------
_AI_SYSTEM = (
    "You read a document an admin uploaded (it may be a spreadsheet rendered as "
    "a pipe-delimited table, CSV, JSON, or free prose) and extract the software "
    "work items (bugs / requirements / tasks) it describes. Return ONE JSON "
    "object and nothing else:\n"
    '  {"bugs": [{"title": str, "description": str, "priority": '
    '"Low|Medium|High|Critical", "item_type": "Bug|Requirement|Task", '
    '"environment": "DEV|UAT|PROD"}]}\n'
    "Rules: the FIRST row of a table is usually a header naming the columns — "
    "use it to map fields, never emit the header itself as an item. Use the "
    "document's own wording for each title. Read every data row. Infer "
    "priority/type/environment when the text implies them, else use Medium / "
    "Bug / DEV. Skip headings, preamble, blank rows and anything that isn't an "
    "actual work item. Never invent items that aren't in the document."
)


def ai_extract_specs(text: str) -> Optional[list[dict]]:
    """Use the cloud assistant to read the document, when it's configured.
    Returns cleaned specs, or None when the layer is off / unavailable / it
    produced nothing usable (so the caller falls back to the parser)."""
    try:
        from app.chatbot import cloud_llm
        if not cloud_llm.is_available():
            return None
        parsed = cloud_llm.complete_json(_AI_SYSTEM, text[:20000])
    except Exception:  # noqa: BLE001 — an AI fault must never break ingest
        logger.exception("Sleuth ingest AI extraction failed; using parser")
        return None
    if not parsed or not isinstance(parsed.get("bugs"), list):
        return None
    specs = _clean_all(parsed["bugs"])
    return specs or None


def _document_text(filename: str, raw: bytes) -> str:
    """A readable plain-text representation of the upload for the AI reader.
    A spreadsheet becomes a pipe-delimited table (so the model sees the rows,
    headers and all); everything else is decoded as UTF-8."""
    if _looks_like_xlsx(filename or "", raw):
        rows = _xlsx_rows(raw)
        if not rows:
            return ""
        return "\n".join(" | ".join(c.strip() for c in r) for r in rows)
    return raw.decode("utf-8", errors="replace")


def extract_specs(filename: str, raw: bytes) -> tuple[list[dict], str]:
    """Top-level extraction: let the AI read the document first (it handles
    headers, messy layouts and prose far better than the parser), falling back
    to the deterministic parser when the AI layer is off or unhelpful. Returns
    (specs, method) where method is 'ai' or 'parser'."""
    text = _document_text(filename, raw)
    if text.strip():
        ai = ai_extract_specs(text)
        if ai:
            return ai, "ai"
    return parse_document(filename, raw), "parser"


def resolve_project_for_preview(db: Session, project_id: Optional[int]) -> Optional[Project]:
    """Resolve the project the ingested items would land in (for the preview),
    without creating anything. None when there's no project at all."""
    return _resolve_project(db, project_id)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
def _resolve_project(db: Session, project_id: Optional[int]) -> Optional[Project]:
    if project_id is not None:
        return db.get(Project, project_id)
    # Default to the lowest-id project (the bootstrap "General" project), so an
    # admin can ingest without first picking a project.
    return db.scalar(select(Project).order_by(Project.id))


def create_bugs_from_specs(
    db: Session, specs: list[dict], actor: User, project_id: Optional[int] = None,
) -> dict:
    """Create a Bug per spec under one project, reported by the admin, each
    audited. Returns a summary dict {created, project_id, project_name, items}.
    Raises ValueError if the target project can't be resolved."""
    project = _resolve_project(db, project_id)
    if project is None:
        raise ValueError("No project to file these items under — create a project first")

    created: list[dict] = []
    for spec in specs[:MAX_SPECS]:
        bug = Bug(
            project_id=project.id,
            reporter_id=actor.id,
            title=spec["title"],
            description=sanitize_html(spec.get("description", "")),
            item_type=spec.get("item_type", "Bug"),
            status="New",
            priority=spec.get("priority", "Medium"),
            environment=spec.get("environment", "DEV"),
        )
        db.add(bug)
        db.flush()
        db.add(Activity(
            bug_id=bug.id, entity_type="bug", entity_id=bug.id,
            actor_user_id=actor.id, actor_name=actor.name, action="bug_created",
            detail=f"{bug.item_type} #{bug.id} '{bug.title}' created via Sleuth document ingest",
        ))
        created.append({"id": bug.id, "title": bug.title})
    db.commit()
    return {
        "created": len(created),
        "project_id": project.id,
        "project_name": project.name,
        "items": created,
    }


__all__ = [
    "parse_document", "ai_extract_specs", "extract_specs",
    "create_bugs_from_specs", "resolve_project_for_preview",
    "MAX_SPECS", "MAX_DOC_BYTES",
]
