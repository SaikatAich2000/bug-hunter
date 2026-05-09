# Bug Hunter — v3.1 release notes

This release narrows the role policy on destructive actions, adds
admin-only session management (Keycloak-style), and replaces the
two-modal "list → detail → edit" bug flow with a single Jira-style
inline screen.

The schema change is **purely additive**: one new `sessions` table.
No existing table's columns, types, indexes or constraints change.
You can roll forward and back without risk to existing rows.

---

## What changed (by your numbered list)

### 1 & 2 & 3. Role policy

| Action                       | Before        | After (v3.1)               |
|------------------------------|---------------|----------------------------|
| Bug — create / edit / reassign | mixed by ownership | any authenticated user |
| Bug — delete                 | admin + manager | **admin only** |
| Project — create / edit      | admin + manager | admin + manager (unchanged) |
| Project — delete             | admin + manager | **admin only** |
| User — create / edit         | admin only      | admin + manager |
| User — delete                | admin only      | admin only (unchanged) |
| User — grant admin role      | admin only      | admin only (manager blocked, even on create) |
| User — edit / disable an admin | admin only    | admin only (manager blocked) |
| Audit Trail                  | every user      | **admin + manager only** |
| Sessions — list + revoke     | (didn't exist)  | **admin only** |

Backend enforcement (always the source of truth) lives in
`app/auth.py` (the `can_*` helpers) and the FastAPI dependencies
`require_admin` / `require_manager_or_admin`. Frontend visibility uses
`data-needs-role` attributes plus a defensive
`[data-needs-role] { display: none }` CSS rule, so role-gated UI never
flashes visible before the auth check resolves.

### 4. Per-session management (Keycloak-style)

A new `sessions` table tracks every active server-side session keyed
by a unique `jti` baked into the signed cookie. On every authenticated
request the jti is looked up; if the row is missing or expired, the
cookie is rejected. This is what makes per-session revocation possible
— the existing `session_version` mechanism logs out *every* device for
a user; revoking a session row logs out only the one device.

UI: new **Sessions** sidebar item, sits below Audit Trail, admin-only.
Lists every active session with user, role, IP, short browser+OS hint,
created / last-seen / expires timestamps, and a Revoke button per row.
The admin's own current session is flagged "This is you" and the
Revoke button is disabled (use Log out for that). The API also
hard-rejects revoking your own current session as a safety net.

Side effects:

- Login: creates one session row, captures IP + user-agent.
- Logout: deletes the row keyed by the cookie's jti. Other sessions
  for the same user untouched.
- Change password / reset password: bumps `session_version` (which
  invalidates every previously-issued token for that user) AND deletes
  every session row for that user, then re-establishes a fresh row +
  cookie for the device that just changed the password so the user
  isn't bounced to login by their own action.
- `last_seen_at` is updated at most once per minute per session so the
  request hot-path stays cheap.
- Expired session rows are swept on read by the admin list endpoint.

Backward compatibility: tokens issued by v3.0 builds don't carry a
jti. The auth layer accepts those legacy tokens and treats them as
authenticated; they just don't appear in the admin session list. As
soon as a user logs in fresh under v3.1, their next session has a jti
and shows up.

### 5. Audit Trail hidden from regular users

`/api/audit` now requires manager+ via `require_manager_or_admin`;
plain users get a 403. The sidebar nav button has
`data-needs-role="manager"` so it's not even shown to them.

### 6. Single-screen Jira-style bug detail

The old flow was: list → row click opens read-only detail modal with
4 tabs → click pencil/edit → small modal with editable form. Two
separate modals, four tabs, no editing inline.

v3.1 collapses that to one modal:

- Click any row → unified bug modal opens with all fields editable
  inline.
- Title is a full-width input at the top.
- Two-column body (Jira-style): main column has description,
  comments, attachments, activity (collapsible); side rail has
  status, priority, environment, project, reporter, assignees, due
  date, plus read-only Created / Updated timestamps.
- Comments and attachment uploads are inline — no tab to switch to.
- Save button at the bottom; on save, sections re-render in place.
- Delete button in the modal header — admin-only.
- Pencil edit button on table rows is gone. Row click is the only way.

### 7. Modal width

The bug modal is now `.modal-card.xxl` — `width: 95vw`, `max-width:
1400px`, `max-height: 92vh` on desktop. Wider than 90%, never 100%, as
specified. Responsive collapse:

- ≤ 900 px : two-column body collapses to one column (side rail
            stacks below the main content).
- ≤ 700 px : modal goes full viewport width (max-width: 100%).
- ≤ 500 px : modal goes full screen (no border radius, full height) —
            the same rule the other modals already had.

---

## Schema changes — additive only

One new table:

```sql
CREATE TABLE sessions (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    jti           VARCHAR(64) NOT NULL UNIQUE,
    user_agent    VARCHAR(400) NOT NULL DEFAULT '',
    ip_address    VARCHAR(64) NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL,
    last_seen_at  TIMESTAMPTZ NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_sessions_jti        ON sessions(jti);
CREATE INDEX idx_sessions_user_id    ON sessions(user_id);
CREATE INDEX idx_sessions_expires_at ON sessions(expires_at);
```

`init_db()` calls `Base.metadata.create_all(bind=engine)` which is
idempotent and only creates tables that don't already exist. So:

- On a v3.0 → v3.1 deploy, the new `sessions` table is created the
  first time the v3.1 image starts. Nothing else changes.
- On any subsequent restart, `create_all()` sees the table is already
  there and does nothing.

No existing table is altered. No column type changes. No index drops.

---

## Live-data safety checklist

- [x] No DDL on existing tables. Only the new `sessions` table is
      created (idempotent — `IF NOT EXISTS` semantics from
      `create_all`).
- [x] All existing status / priority / environment / role values
      remain accepted, so any pre-existing row deserializes cleanly.
- [x] Existing session cookies (no jti) keep authenticating after the
      deploy — verified by `parse_session_token` accepting both
      2-part and 3-part tokens.
- [x] `users` row layout unchanged — `session_version` was already
      there in v3.0.
- [x] `bugs.status` column type unchanged (still `String(20)`).
- [x] Cookie name (`bh_session`), session secret, bootstrap admin
      flow — all untouched.
- [x] No environment variables added or renamed in
      `docker-compose.yml` or `.env.example`.
- [x] `./deploy.sh` rebuilds the image and restarts containers; it
      does **not** touch the named volume `bugtracker_pgdata`.
- [x] `./down.sh` (no flags) keeps the volume. Only `./down.sh
      --wipe-db` removes data, and it asks the operator to type `YES`
      first.

---

## Tests

```
pytest tests/                # 225 passing (197 v3.0 + 28 new for v3.1)
```

The new tests live in `tests/test_v31.py` and cover:

- Bug delete admin-only (admin allowed; manager 403; user 403; even
  the user who reported the bug gets 403).
- Users can edit any bug, including changing assignees on someone
  else's bug.
- `can_edit` flag is True for every authenticated user on every bug.
- Project delete admin-only (manager 403); manager can still create
  and edit; users can do neither.
- Manager can create + edit users, but cannot grant the admin role,
  cannot edit existing admins, cannot delete users.
- Audit endpoint: 403 for user, 200 for manager and admin.
- Sessions endpoint: 403 for user and manager, 200 for admin. List
  carries `is_current` flag and user metadata. Revoke kills the
  target's cookie immediately. Admin can't revoke their own current
  session. Admin CAN revoke their own session on a different device.
  Logout removes the session row.
- Password change keeps the current device authenticated (jti
  re-issued).

Two pre-existing tests were updated to assert the new behavior
rather than the old:

- `test_user_cannot_edit_others_bugs` →
  `test_user_can_edit_others_bugs_but_cannot_delete`
- `test_audit_endpoint_is_visible_to_any_user` → split into
  `test_audit_endpoint_is_hidden_from_regular_users` +
  `test_audit_endpoint_visible_to_admin`.

---

## Deploy steps

This is a code-only deploy. The DB migration is automatic and additive.

```bash
# 1) On the server, in your bug-hunter checkout, stop the running stack.
#    (Plain `down`, NOT `--wipe-db`. The Postgres volume bugtracker_pgdata
#    is preserved.)
./down.sh

# 2) Replace the source tree with the contents of bug-hunter.zip.
#    Preserve your existing .env file — DO NOT overwrite it.
unzip -o bug-hunter.zip   # the zip extracts INTO ./bug-hunter/

# 3) Re-deploy. This rebuilds the image and restarts the stack.
#    Postgres data lives in the named volume `bugtracker_pgdata`
#    and is NOT touched.
./deploy.sh
```

After it comes up:

- `/api/health` returns `200 ok`.
- The brand area shows version `v3.1.0`.
- Existing user sessions still work (legacy cookie compat).
- A fresh login, then **Sessions** in the sidebar → you should see
  your own session with "This is you".

## Rollback

If anything goes sideways, redeploying the previous zip is enough.
The DB needs no rollback — the only schema change is the new
`sessions` table, which a v3.0 build harmlessly ignores. You can
leave it in place; or run `DROP TABLE sessions;` from psql if you
want to reclaim the space.
