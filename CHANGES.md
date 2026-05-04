# Bug Hunter — change notes for this release

This patch is **fully additive** with respect to the database. No DDL is run,
no columns change type, no indexes drop. You can roll forward and back without
risk to existing rows.

## What changed (by your numbered list)

### 1. Two new statuses

`ALLOWED_STATUSES` in `app/schemas.py` now contains:

```
New, In Progress, Resolved, Closed, Reopened, Not a Bug, Resolve Later
```

The `bugs.status` column is `String(20)` — both new values fit (`"Resolve Later"` is 13 chars).

### 2. KPI strip rebuilt

The dashboard now shows: **Total · Open · Resolved · Closed · Resolve Later**.
"Users" and "Projects" KPIs are removed from the UI.

`/api/stats` returns the new fields (`closed`, `resolve_later`) in addition
to the existing ones. `users` and `projects` stay in the response (defaulting
to 0 in the schema) so any older cached SPA doesn't crash on the field
disappearing.

**Important rule:** rows with `status="Not a Bug"` are **excluded from the
`bugs` total** because the spec says "Not a Bug means the bug is actually
not a bug so we don't count it." They are still preserved in the DB and
still appear in the per-status breakdown chart on the analytics page.

### 3. Multi-select filters

All five filter-bar dropdowns (Project, Status, Priority, Environment,
Assignee) accept multiple selections. The API change is backward-compatible:

- Old call: `?status=New` → still works (parsed as a 1-element list).
- New call: `?status=New&status=Resolved` → OR-match.

Each repeated value is normalized case-insensitively and validated; one
unknown value 400s the whole request.

The sidebar Project / Assignee click-to-filter shortcut now toggles
membership in the multi-select array (clicking the same project twice
removes it).

### 4. Reporter name spillover during edit

The Reporter field is now on its own row in the bug create/edit modal.
Option labels show only the user's name (the email moved to a `title`
tooltip) and the select has `max-width: 100%`, `width: 100%` so a long
display name can no longer push other fields out of the modal.

### 5. "Updated" column removed; Title gets the space

The bug table's `Updated` column is gone. The freshness signal isn't
lost — `updated_at` is rendered as a small muted line under each title
("Updated 3 days ago"). The Title column has `width: auto` and a larger
font, and the table's `min-width` dropped from 900 → 820 px so it fits
on smaller laptops without horizontal scroll.

### 6. Collapsible sidebar

A `«` button in the brand area toggles the sidebar between full (280 px)
and rail (60 px) widths. State is persisted to `localStorage` under the
key `sidebarCollapsed` so the layout survives reload. On screens
≤ 900 px the sidebar reverts to the existing off-canvas mobile drawer.

### 7. Performance — for low-resource VMs

Three changes:

1. **GZip middleware** (`fastapi.middleware.gzip.GZipMiddleware`,
   `minimum_size=1024`, `compresslevel=5`). Compresses HTML / JS / CSS /
   JSON over the wire. Skipped for already-compressed media (images,
   video, attachment downloads which set their own `Cache-Control`).

2. **N+1 fix in `GET /api/bugs`** — previously the listing endpoint
   issued one extra `SELECT count(*)` per bug to compute
   `attachment_count`. With `page_size=50` that was 50 extra round-trips.
   Now a single aggregate query keyed by `bug_id` returns all counts at
   once. There's a regression test in `tests/test_changes.py::TestAttachmentCountPerf`
   that asserts the listing endpoint stays under 15 SQL queries for 10
   bugs.

3. **Static-asset caching** is unchanged (was already 1-year, immutable
   via the asset-version hash). Combined with gzip the SPA shell now
   ships in ~25 KB compressed instead of ~80 KB.

No worker count, image size, or compose memory limit was changed — your
existing 512 MB cap stays valid.

### 8. Responsive

Breakpoints reviewed end-to-end:

- ≤ 1100 px : KPI strip drops to 3 columns (5th wraps).
- ≤ 900 px  : sidebar becomes off-canvas drawer; layout stacks.
- ≤ 700 px  : 2-column KPIs; multi-select buttons go 50% wide; the table
              hides Env and Attachment-count columns.
- ≤ 500 px  : KPIs go 2-up with the 5th spanning both; modals go full-screen;
              table additionally hides Priority and Assignees columns and
              its `min-width` drops to 480 px so phones don't horizontal-scroll.

The collapsible sidebar respects the mobile breakpoint — collapsed-rail
mode only applies above 900 px.

## Live-data safety checklist

- [x] No DDL; `Base.metadata.create_all()` is idempotent and adds nothing
      because every table already exists in your DB.
- [x] `status` column type unchanged (`String(20)`); both new values fit.
- [x] All existing status values remain in `ALLOWED_STATUSES`, so any
      pre-existing row deserializes cleanly. Test:
      `tests/test_changes.py::TestBackwardCompat::test_existing_bug_with_old_status_still_listable`.
- [x] `?status=New` (the legacy single-value query) still returns the
      expected results. Test:
      `tests/test_changes.py::TestBackwardCompat::test_old_single_status_query_unchanged`.
- [x] `users` and `projects` are still present in `/api/stats`, defaulting
      to 0 so older cached SPA copies don't crash before reload.
- [x] Cookie name, session secret, bootstrap admin flow — all untouched.
- [x] No environment variables added or renamed in `docker-compose.yml`
      or `.env.example`.

## Tests

```
pytest tests/                # 196 passed (174 pre-existing + 22 new)
```

The new tests live in `tests/test_changes.py` and cover:

- New statuses round-trip through create / list / filter.
- KPI shape and per-status counting (Not a Bug excluded from total,
  still appears in `by_status`).
- Multi-select OR-matching, empty-string ignoring, and 400 on bogus values.
- Backward compat (old single-value queries, old statuses).
- Attachment-count perf (single aggregate query, not N+1).

## Deploy steps

This is a code-only deploy. No DB migration, no config change.

```bash
# 1) On the server, in the bug-hunter checkout:
./down.sh

# 2) Replace the source tree with the contents of bug-hunter.zip
#    (preserve your existing .env file — DO NOT overwrite it).
unzip -o bug-hunter.zip   # the zip extracts INTO ./bug-hunter/

# 3) Re-deploy. This rebuilds the image and restarts the stack.
#    Postgres data lives in the named volume `bugtracker_pgdata` and is
#    NOT touched.
./deploy.sh
```

After it comes up:

- `/api/health` should return `200 ok`.
- The header bar should show 5 KPIs (no Users / Projects tiles).
- The filter bar dropdowns should accept multiple checks.
- The brand area should have a `«` collapse button.

## Rollback

If anything goes sideways, redeploying the previous zip is enough. The DB
needs no rollback — nothing was migrated.
