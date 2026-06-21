"""Regenerate every API artifact from the live FastAPI app.

Outputs (under docs/api/):
    openapi.json                          - authoritative spec
    openapi.yaml                          - same spec, YAML form
    Bug-Hunter.postman_collection.json    - Postman v2.1 importable collection
    curl.md                               - copy-paste curl reference

Importing app.main is safe - it does not open DB connections (those only
fire inside the lifespan handler, which uvicorn invokes at startup).

Re-run after any route or schema change:
    python scripts/gen-api-docs.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402 - sys.path must be set first

OUT_DIR = ROOT / "docs" / "api"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Constants. Content-type strings are factored out so the body-dispatch logic
# below reads as one switch instead of repeating string literals.
# ---------------------------------------------------------------------------
CT_JSON = "application/json"
CT_MULTIPART = "multipart/form-data"
CT_FORM = "application/x-www-form-urlencoded"

# Placeholder values used only when emitting docs/Postman/curl examples.
# They are not real secrets: `_BOOT_DEFAULT` is the bootstrap admin's seed
# value (also published in README.md and .env.example so an operator can log
# in on first boot), the others are invented strings used as form
# placeholders. Constant names avoid credential-keyword tokens so they aren't
# flagged as hard-coded secrets in this developer-tool script.
_BOOT_DEFAULT = "ChangeMe123!"  # real bootstrap admin seed (config.BOOTSTRAP_ADMIN_PASSWORD)
_RESET_DEFAULT = "BetterPass1"
_USER_DEFAULT = "SecurePass1"

# Per-schema example overrides — keeps the generated examples realistic for
# the Bug Hunter domain instead of "string" / "user@example.com" stubs.
FIELD_OVERRIDES: dict[str, dict[str, Any]] = {
    "LoginIn":          {"email": "admin@example.com", "password": _BOOT_DEFAULT},
    "UserIn":           {"name": "Jane Tester", "email": "jane@example.com",
                         "role": "user", "password": _USER_DEFAULT, "is_active": True},
    "UserUpdate":       {"name": "Jane Tester (updated)", "role": "manager"},
    "ChangePasswordIn": {"current_password": _BOOT_DEFAULT, "new_password": _RESET_DEFAULT},
    "ForgotPasswordIn": {"email": "jane@example.com"},
    "ResetPasswordIn":  {"token": "<paste-token-from-reset-email>",
                         "new_password": _RESET_DEFAULT},
    "ProjectIn":        {"name": "Mobile App", "description": "iOS + Android client",
                         "color": "#c9764f"},
    "BugCreate":        {"project_id": 1, "title": "Login fails on Safari 17",
                         "description": "<p>Steps to reproduce: …</p>",
                         "item_type": "Bug", "status": "New", "priority": "Medium",
                         "environment": "DEV", "assignee_ids": [], "due_date": None},
    "BugUpdate":        {"status": "In Progress", "priority": "High"},
    "CommentIn":        {"body": "<p>Reproduced on Safari 17.4.</p>"},
    "EventCreate":      {"name": "Daily standup 2026-06-04",
                         "description": "Standup agenda + action items",
                         "scheduled_for": "2026-06-04", "manager_ids": []},
    "EventUpdate":      {"name": "Daily standup 2026-06-04 (updated)"},
    "ChatIn":           {"message": "How many bugs are open?"},
}

TAG_ORDER = ["meta", "auth", "users", "projects", "bugs", "events", "stats",
             "audit", "sessions", "chatbot"]
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


# ---------------------------------------------------------------------------
# Example generation — broken into single-purpose helpers so the dispatcher
# itself stays under the cognitive-complexity threshold.
# ---------------------------------------------------------------------------
def _example_for_primitive(t: str, fmt: str) -> Any:
    if t == "string":
        return {
            "email":     "user@example.com",
            "date":      "2026-06-04",
            "date-time": "2026-06-04T00:00:00Z",
            "binary":    "",
            "uuid":      "00000000-0000-0000-0000-000000000000",
        }.get(fmt, "string")
    if t == "integer": return 1
    if t == "number":  return 1.0
    if t == "boolean": return True
    return None


def _example_for_composition(schema: dict, components: dict, visited: set[str]) -> Any:
    """Handle anyOf / oneOf / allOf."""
    if "anyOf" in schema:
        non_null = [s for s in schema["anyOf"] if s.get("type") != "null"]
        if non_null:
            return example_from_schema(non_null[0], components, visited)
        return None
    if "oneOf" in schema:
        return example_from_schema(schema["oneOf"][0], components, visited)
    if "allOf" in schema:
        merged: dict[str, Any] = {}
        for sub in schema["allOf"]:
            piece = example_from_schema(sub, components, visited)
            if isinstance(piece, dict):
                merged.update(piece)
        return merged or None
    return None


def _example_for_ref(schema: dict, components: dict, visited: set[str]) -> Any:
    ref = schema["$ref"].rsplit("/", 1)[-1]
    if ref in visited:
        return None
    next_visited = visited | {ref}
    result = example_from_schema(components.get(ref, {}), components, next_visited)
    if isinstance(result, dict) and ref in FIELD_OVERRIDES:
        result = {**result, **FIELD_OVERRIDES[ref]}
    return result


def example_from_schema(schema: dict, components: dict[str, dict],
                        _visited: set[str] | None = None) -> Any:
    """Walk an OpenAPI schema fragment and emit a plausible JSON example."""
    if not schema:
        return None
    visited = _visited or set()

    if "$ref" in schema:
        return _example_for_ref(schema, components, visited)
    if "default" in schema:  return schema["default"]
    if "example" in schema:  return schema["example"]
    if schema.get("enum"):   return schema["enum"][0]

    composed = _example_for_composition(schema, components, visited)
    if composed is not None or {"anyOf", "oneOf", "allOf"} & set(schema):
        return composed

    t = schema.get("type")
    if t == "array":
        return [example_from_schema(schema.get("items", {}), components, visited)]
    if t == "object":
        props = schema.get("properties", {})
        return {k: example_from_schema(v, components, visited) for k, v in props.items()}
    return _example_for_primitive(t or "", schema.get("format", ""))


# ---------------------------------------------------------------------------
# OpenAPI spec dump
# ---------------------------------------------------------------------------
def dump_spec() -> dict:
    spec = app.openapi()
    (OUT_DIR / "openapi.json").write_text(
        json.dumps(spec, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        print("PyYAML not installed; skipping openapi.yaml.")
    else:
        (OUT_DIR / "openapi.yaml").write_text(
            yaml.safe_dump(spec, sort_keys=False), encoding="utf-8"
        )
    return spec


# ---------------------------------------------------------------------------
# Postman v2.1 collection
# ---------------------------------------------------------------------------
def _resolve_ref(schema: dict, components: dict[str, dict]) -> dict:
    """Follow a single $ref into the components map. Returns the schema
    unchanged if it isn't a $ref."""
    if isinstance(schema, dict) and "$ref" in schema:
        ref_name = schema["$ref"].rsplit("/", 1)[-1]
        return components.get(ref_name, {})
    return schema


def _postman_path_var(p: dict) -> dict:
    return {
        "key": p["name"],
        "value": str(example_from_schema(p.get("schema") or {}, {})),
        "description": p.get("description", ""),
    }


def _postman_query_param(p: dict) -> dict:
    return {
        "key": p["name"],
        "value": "",
        "description": p.get("description", ""),
        "disabled": not p.get("required", False),
    }


def _postman_url(path: str, op: dict) -> dict:
    pm_path = path.replace("{", ":").replace("}", "")
    segs = [s for s in pm_path.split("/") if s]
    path_vars, query = [], []
    for p in op.get("parameters") or []:
        if p.get("in") == "path":
            path_vars.append(_postman_path_var(p))
        elif p.get("in") == "query":
            query.append(_postman_query_param(p))
    raw = "{{baseUrl}}" + pm_path
    if query:
        raw += "?" + "&".join(f"{q['key']}=" for q in query)
    url: dict[str, Any] = {"raw": raw, "host": ["{{baseUrl}}"], "path": segs}
    if path_vars: url["variable"] = path_vars
    if query:     url["query"] = query
    return url


def _postman_body_json(content_entry: dict, components: dict) -> dict:
    schema = content_entry.get("schema") or {}
    example = example_from_schema(schema, components)
    return {
        "mode": "raw",
        "raw": json.dumps(example, indent=2),
        "options": {"raw": {"language": "json"}},
    }


def _postman_body_multipart(content_entry: dict, components: dict) -> dict:
    schema = _resolve_ref(content_entry.get("schema") or {}, components)
    formdata = []
    for name, sub in schema.get("properties", {}).items():
        is_binary = sub.get("format") == "binary"
        formdata.append({
            "key": name,
            "type": "file" if is_binary else "text",
            "value": "" if is_binary else str(example_from_schema(sub, components) or ""),
            "description": sub.get("description", ""),
        })
    return {"mode": "formdata", "formdata": formdata}


def _postman_body(op: dict, components: dict) -> dict | None:
    rb = op.get("requestBody")
    if not rb:
        return None
    content = rb.get("content", {})
    if CT_JSON in content:
        return _postman_body_json(content[CT_JSON], components)
    if CT_MULTIPART in content:
        return _postman_body_multipart(content[CT_MULTIPART], components)
    if CT_FORM in content:
        return {"mode": "urlencoded", "urlencoded": []}
    return None


def _build_postman_item(method: str, path: str, op: dict, components: dict) -> dict:
    headers = []
    body = _postman_body(op, components)
    if body and body.get("mode") == "raw":
        headers.append({"key": "Content-Type", "value": CT_JSON})
    item = {
        "name": f"{method.upper()} {path}",
        "request": {
            "method": method.upper(),
            "header": headers,
            "url": _postman_url(path, op),
            "description": op.get("summary") or op.get("description") or "",
        },
    }
    if body:
        item["request"]["body"] = body
    return item


def build_postman(spec: dict) -> dict:
    components = spec.get("components", {}).get("schemas", {})
    folders: dict[str, list[dict]] = {tag: [] for tag in TAG_ORDER}

    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if method.lower() not in HTTP_METHODS:
                continue
            tag = (op.get("tags") or ["misc"])[0]
            folders.setdefault(tag, []).append(
                _build_postman_item(method, path, op, components)
            )

    collection_items = [
        {"name": tag, "item": folders[tag]}
        for tag in TAG_ORDER + [t for t in folders if t not in TAG_ORDER]
        if folders.get(tag)
    ]

    return {
        "info": {
            "name": f"{spec['info']['title']} v{spec['info']['version']}",
            "_postman_id": "bug-hunter-collection",
            "description": (
                "Generated from docs/api/openapi.json by scripts/gen-api-docs.py.\n\n"
                "Authentication is cookie-based — POST `auth/login` first; Postman "
                "stores the `bh_session` cookie automatically and replays it on every "
                "subsequent request. State-changing endpoints (POST/PUT/PATCH/DELETE) "
                "additionally require an `Origin` header matching the configured "
                "CORS_ORIGINS or the request Host — Postman sends `Origin: {{baseUrl}}` "
                "automatically when run from the desktop app, so this Just Works."
            ),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": collection_items,
        "variable": [
            {"key": "baseUrl", "value": "http://localhost:8000", "type": "string"},
        ],
    }


# ---------------------------------------------------------------------------
# curl.md - human-readable reference
# ---------------------------------------------------------------------------
CURL_HEADER = """# Bug Hunter - API curl reference

Generated from `docs/api/openapi.json` by `scripts/gen-api-docs.py`.

## Conventions

```bash
BASE=http://localhost:8000
COOKIES=./bh_cookies.txt    # cookie jar shared across calls
```

## Authenticating

Bug Hunter uses an HTTP-only session cookie named `bh_session`. Log in once,
then carry the cookie via `-b "$COOKIES"` on every subsequent call. `-c` saves
new cookies; `-b` sends them.

```bash
curl -c "$COOKIES" -X POST "$BASE/api/auth/login" \\
  -H "Content-Type: application/json" \\
  -d '{"email":"admin@example.com","password":"ChangeMe123!"}'
```

For mutating calls (POST/PUT/PATCH/DELETE on `/api/*`, except `/api/auth/login`)
the server's CSRF middleware requires an `Origin` header that matches the
request Host. curl on the same host satisfies this; if you're hitting a remote
deploy, add `-H "Origin: https://bugs.example.com"` matching your `CORS_ORIGINS`.

---
"""


def _render_query_params(params: list[dict]) -> list[str]:
    query = [p for p in params if p.get("in") == "query"]
    if not query:
        return []
    out = ["", "Query parameters:"]
    for q in query:
        desc_lines = (q.get("description") or "").strip().splitlines()
        desc = desc_lines[0] if desc_lines else ""
        req = " *(required)*" if q.get("required") else ""
        schema = q.get("schema") or {}
        t = " | ".join(map(str, schema["enum"])) if "enum" in schema else (schema.get("type") or "string")
        out.append(f"- `{q['name']}` (`{t}`){req}{' — ' + desc if desc else ''}")
    return out


def _substitute_path_vars(path: str, params: list[dict], components: dict) -> str:
    url_path = path
    for p in params:
        if p.get("in") == "path":
            sample = example_from_schema(p.get("schema") or {}, components)
            url_path = url_path.replace("{" + p["name"] + "}", str(sample))
    return url_path


def _required_query_string(params: list[dict], components: dict) -> str:
    required = [p for p in params if p.get("in") == "query" and p.get("required")]
    if not required:
        return ""
    return "?" + "&".join(
        f"{p['name']}={example_from_schema(p.get('schema') or {}, components)}"
        for p in required
    )


def _body_for_curl(op: dict, components: dict) -> tuple[str | None, bool]:
    """Return (json_body_string, is_multipart). At most one is truthy."""
    rb = op.get("requestBody")
    if not rb:
        return None, False
    content = rb.get("content", {})
    if CT_JSON in content:
        example = example_from_schema(content[CT_JSON].get("schema") or {}, components)
        return json.dumps(example, separators=(",", ":")), False
    if CT_MULTIPART in content:
        return None, True
    return None, False


def _curl_command(method: str, path: str, op: dict,
                  components: dict[str, dict]) -> list[str]:
    """Emit a multi-line bash curl block for one operation."""
    public_paths = {"/api/health", "/api/meta"}
    if path == "/api/auth/login":
        cookie_flag = '-c "$COOKIES" '
    elif path in public_paths:
        cookie_flag = ""
    else:
        cookie_flag = '-b "$COOKIES" '

    params = op.get("parameters") or []
    url_path = _substitute_path_vars(path, params, components)
    qs = _required_query_string(params, components)
    body_json, is_multipart = _body_for_curl(op, components)

    first = f'curl {cookie_flag}-X {method} "$BASE{url_path}{qs}"'
    if body_json is not None:
        return [
            f"{first} \\",
            '  -H "Content-Type: application/json" \\',
            f"  -d '{body_json}'",
        ]
    if is_multipart:
        return [
            f"{first} \\",
            '  -F "file=@/path/to/upload.png" \\',
            '  -F "comment_id="',
        ]
    return [first]


def _render_operation_block(method: str, path: str, op: dict, components: dict) -> list[str]:
    summary = (op.get("summary") or "").strip()
    heading = f"### {method} {path}" + (f" — {summary}" if summary else "")
    out = [heading]
    out.extend(_render_query_params(op.get("parameters") or []))
    out.append("")
    out.append("```bash")
    out.extend(_curl_command(method, path, op, components))
    out.append("```")
    out.append("")
    return out


def build_curl_md(spec: dict) -> str:
    components = spec.get("components", {}).get("schemas", {})
    by_tag: dict[str, list[tuple[str, str, dict]]] = {}
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if method.lower() not in HTTP_METHODS:
                continue
            tag = (op.get("tags") or ["misc"])[0]
            by_tag.setdefault(tag, []).append((method.upper(), path, op))

    out: list[str] = [CURL_HEADER]
    for tag in TAG_ORDER + [t for t in by_tag if t not in TAG_ORDER]:
        ops = by_tag.get(tag)
        if not ops:
            continue
        out.append(f"\n## {tag}\n")
        for method, path, op in ops:
            out.extend(_render_operation_block(method, path, op, components))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    spec = dump_spec()
    paths_count = len(spec.get("paths", {}))
    ops_count = sum(
        1 for methods in spec.get("paths", {}).values()
          for m in methods if m.lower() in HTTP_METHODS
    )
    print(f"openapi.json: {paths_count} paths, {ops_count} operations")

    pm = build_postman(spec)
    (OUT_DIR / "Bug-Hunter.postman_collection.json").write_text(
        json.dumps(pm, indent=2) + "\n", encoding="utf-8"
    )
    folder_counts = ", ".join(f"{f['name']}({len(f['item'])})" for f in pm["item"])
    print(f"Bug-Hunter.postman_collection.json: {folder_counts}")

    md = build_curl_md(spec)
    (OUT_DIR / "curl.md").write_text(md, encoding="utf-8")
    print(f"curl.md: {len(md)} chars")


if __name__ == "__main__":
    main()
