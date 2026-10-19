"""Seed demo users, projects and work items into a RUNNING local instance via its API.

    python scripts/seed_local.py   (venv python; server must be running)

Everything comes from .env (read through app.config): the server URL
(SEED_BASE_URL, default APP_BASE_URL), the admin login (BOOTSTRAP_ADMIN_EMAIL /
BOOTSTRAP_ADMIN_PASSWORD), the demo users' password (SEED_USER_PASSWORD, default
the admin password) and their e-mail domain (SEED_EMAIL_DOMAIN, default bh.local).
Idempotent: existing users/projects/items (by e-mail/name/title) are skipped.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import env_str, get_settings  # noqa: E402

_ROLES = ("manager", "user")
_PROJECTS = (("Demo Web App", "#c9764f"), ("Demo Mobile App", "#3b82f6"))
_ITEMS = (
    ("Login button unresponsive on Safari", "Bug", "High"),
    ("Add dark mode toggle", "Task", "Medium"),
    ("Crash when uploading a large attachment", "Bug", "Critical"),
    ("Export report to XLSX", "Task", "Low"),
)


def _ok(resp: httpx.Response, what: str) -> dict | list:
    if resp.status_code >= 400 and resp.status_code != 409:
        raise SystemExit(f"{what} failed: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json() if resp.content else {}


def main() -> None:
    s = get_settings()
    base = (env_str("SEED_BASE_URL", "") or s.APP_BASE_URL).rstrip("/")
    domain = env_str("SEED_EMAIL_DOMAIN", "bh.local")
    password = env_str("SEED_USER_PASSWORD", "") or s.BOOTSTRAP_ADMIN_PASSWORD
    if not (s.BOOTSTRAP_ADMIN_EMAIL and s.BOOTSTRAP_ADMIN_PASSWORD):
        raise SystemExit("Set BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD in .env first.")
    with httpx.Client(base_url=base, timeout=15, headers={"Origin": base}) as c:
        _ok(c.post("/api/auth/login", json={
            "email": s.BOOTSTRAP_ADMIN_EMAIL, "password": s.BOOTSTRAP_ADMIN_PASSWORD}), "login")
        project_ids = []
        existing = {p["name"]: p["id"] for p in _ok(c.get("/api/projects"), "list projects")}
        for name, color in _PROJECTS:
            pid = existing.get(name) or _ok(c.post("/api/projects", json={
                "name": name, "color": color, "agile_enabled": True}), f"project {name}")["id"]
            project_ids.append(pid)
        for role in _ROLES:
            _ok(c.post("/api/users", json={
                "name": f"Demo {role.title()}", "email": f"{role}@{domain}", "role": role,
                "password": password, "project_ids": project_ids}), f"user {role}")
        have = {b["title"] for b in _ok(c.get("/api/bugs", params={"page_size": s.PAGE_SIZE_MAX}), "list items")
                .get("items", [])}
        for i, (title, kind, prio) in enumerate(_ITEMS):
            if title not in have:
                _ok(c.post("/api/bugs", json={
                    "project_id": project_ids[i % len(project_ids)], "title": title,
                    "item_type": kind, "priority": prio}), f"item {title}")
    print(f"Seeded {base}: projects={len(_PROJECTS)} users={[f'{r}@{domain}' for r in _ROLES]} "
          f"items={len(_ITEMS)} (password from .env)")


if __name__ == "__main__":
    main()
