# Changelog

All notable changes to Bug Hunter. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/). The project predates
this file; releases older than v2.4 live in git history only.

## [3.0] — 2026-06-13

**Major release — new frontend, in-app notifications, UX overhaul.** DB
changes are strictly additive (one new `notifications` table created by
`init_db()` on boot; no existing table is altered, so the production database
is untouched on upgrade).

- **Frontend rewritten in React 18 + TypeScript** (Vite multi-page build into
  `app/static`); the backend and its `_serve_html()` contract are unchanged.
- **Per-user in-app notifications.** A new bell in the top bar surfaces the
  events you already get emails for — assigned to an item, a comment on an item
  you report or are assigned to, a status/field change, etc. Notifications are
  strictly per-user and role-respecting (no endpoint exposes another user's
  notifications). New `/api/notifications` API: list, unread count, mark-read,
  read-all, delete.
- **Spectrum Tide visual identity (light + dark).** An ocean-deep base blended
  with the brand logo's colours into a five-colour system — ocean cerulean
  (primary), brand gold, cool steel, walnut and brick-red — colour-coded across
  KPIs (each tile its own hue + numeral), status / priority / environment
  badges, buttons and menus. Plus a real typeface change to **Plus Jakarta
  Sans** (UI) + **JetBrains Mono** (IDs/code), self-hosted as bundled woff2 (no
  external CDN, CSP-safe). Every design-token *name* and DOM hook is preserved —
  only token *values* changed — so behaviour and the backend token tests are
  unchanged.
- **Shell chrome redesign — top-right account cluster.** The top bar now ends
  in a single persistent right-hand cluster: **New**, the **notifications bell**,
  and a new **profile menu** (avatar + name → role/email, change-password, theme
  toggle, log out). The account card, theme button and log-out moved *out of the
  sidebar* into that menu, so the sidebar is navigation + project/people lists
  only. Three chrome bugs fixed in the process: the notification panel now
  renders reliably; the bell sits in the **same place on every view** (it used to
  jump to the left of the title on non-list views because a hidden search box
  still matched a CSS sibling rule); and the account controls are where the v3.0
  mockups always put them — top-right, not the side rail.
- **Notifications bell** in the top bar (every view) with a live unread badge
  and a dropdown panel: per-user list, mark-read, mark-all-read, dismiss, and
  click-through to the linked item. The unread count rides the existing 15s
  session poll.
- **Optional daily email digest.** Set `EMAIL_DIGEST_ENABLED=true` to stop the
  per-operation work-item emails (new item / update / assignment / comment /
  event) firing one-at-a-time, and instead batch each user's operations into
  **one grouped email per day**, in five sections (assigned / reported / updates
  / comments / events). A standalone job — `python -m app.jobs.email_digest` —
  runs from host cron / Task Scheduler; it's idempotent (stamps each operation
  via the new nullable `notifications.emailed_at` column as it sends, so it
  never double-sends) and window-bounded (`EMAIL_DIGEST_LOOKBACK_HOURS`, default
  26h, so the first run can't replay history). It reuses the notification rows
  that already power the bell, so it's per-user and role-respecting for free.
  **Password-reset and other security/transactional emails are never batched —
  they always send immediately.** Default off, so existing immediate-email
  behaviour is unchanged until ops opts in. Schema change is additive (one
  nullable column); production data untouched.
- **Web push notifications (Firebase Cloud Messaging), optional.** Browser push
  for the five operations (new item / update / assignment / comment / event),
  sent **immediately** when the operation happens — the same trigger points as
  the in-app bell, independent of the email digest. New additive
  `push_subscriptions` table keyed on the FCM **registration token** + a
  `platform` column, so a future native Android app registers into the same
  table and send path with no backend rework. Per-user and role-respecting (it
  mirrors the notification recipients), with dead-token pruning. Frontend: a
  self-hosted Firebase compat SDK (vendored to `app/static/vendor`, so the CSP
  stays `script-src 'self'` — no CDN), a `/firebase-messaging-sw.js` background
  service worker, and an **"Enable push notifications"** toggle in the profile
  menu. The CSP relaxes `connect-src` for FCM's endpoints only while
  `WEB_PUSH_ENABLED` is on. Default off; `firebase-admin` is only exercised when
  enabled and configured with a Firebase service account. See README →
  *Web push notifications* for the one-time Firebase setup.
- **Login "Version" line** moved below the sign-in card; the **Sleuth panel**
  is now resizable (drag grip + expand/shrink, size persisted).
- **Security hardening** (additive, no schema change): IP rate limits extended
  to `reset-password` and `change-password` (the latter bcrypt-verifies on every
  call — an auth-amplification vector); a per-user rate limit on commenting (each
  comment fans out notifications + emails); the password-reset "no account" audit
  line now masks the email like every other auth log; `assignee_ids` /
  `manager_ids` request lists are length-capped; the logout cookie is cleared
  with the same flags it was set with.
- **Performance** (additive, no schema change): two new indexes on `activity_log`
  (`(action, bug_id)` for the resolution/throughput reports, `actor_user_id` for
  the audit "filter by actor" screen) created the additive `init_db()` way; the
  dashboard timeline filter is now sargable so it uses the `created_at` index; the
  audit trail pages in 300-row chunks instead of loading 5000 rows up front.
- **Sleuth got smarter:** it now understands **bare (unquoted) free-text search**
  after a topic cue ("bugs about login crash" → searches "login crash"), with a
  guard so a pure filter phrase ("high priority") isn't mistaken for a topic; and
  the unknown-intent fallback offers a classifier-ranked **"did you mean"**
  suggestion. Both are rule-based/statistical — no network or LLM required.
- **SonarQube cleanup (11 issues + build-output hotspots).** Fixed every open
  finding in the Sleuth code: extracted the duplicated `"bug_hunter.sleuth"`
  logger name into a constant (S1192); split `rag.index_all` into focused
  helpers to drop its cognitive complexity from 21 to under the limit (S3776);
  removed the unused `db`/`actor` parameters from `rag.retrieve_text` (S1172);
  documented the 429/404 `HTTPException`s, dropped the redundant `response_model`,
  and moved the chat router's FastAPI dependencies to `Annotated` form
  (S8415/S8409/S8410); and gave `BugOut.due_date` an explicit `None` default
  (S8396, also a reliability fix). Pruned 25 stale Vite bundles from
  `app/static/assets` and excluded that generated build output from the scan —
  the source lives in `frontend/`, so scanning the minified bundle only produced
  un-actionable findings and hotspots on React's compiled `innerHTML` writes
  (already safe: the HTML is sanitized server-side before storage).
- Roles and permissions are unchanged from v2.10.

## [2.10] — 2026-06-11

**Maintenance + quality pass.** No DB schema changes (migrations remain
strictly additive).

- SonarQube cleanup: resolved all open issues and security hotspots.
- Expanded automated test coverage.

## [2.9] — 2026-06-09

**Reports view + Sleuth report intent.** No DB schema change — the
"who resolved this item and when" attribution is derived from the
existing `activity_log` table, so the production database is
byte-for-byte untouched on upgrade.

- *Reports sidebar view* — Jira-style report builder behind a manager /
  admin role gate (`data-needs-role="manager"`). Nine report types:
  Item Detail Export, Resolution Throughput, Pending Items Snapshot,
  Status Distribution, Priority Distribution, Project Breakdown,
  Aging, Created-vs-Resolved Timeline, Time to Resolution. Universal
  filter set (date range, item types, statuses, priorities,
  environments, projects, assignees, reporters, free text).
- *Excel export* — multi-sheet XLSX with the aggregated view, a
  drill-down "Items" sheet (raw rows for the manager who wants
  everything), and an audit "Filters Applied" sheet. CSV-injection
  defense (G2) migrated from the CSV export to the XLSX writer.
- *Sleuth report intent* — natural-language report queries reuse the
  same engine. *"report of who solved how many bugs last week"*,
  *"pending bugs report"*, *"throughput last 7 days"* etc. produce an
  inline preview plus a downloadable spreadsheet.
- *Legacy CSV export retired* — `/api/bugs/export.csv` removed; the
  sidebar "Export CSV" button replaced by the Reports entry. Tests
  that exercised the CSV path moved to the XLSX path with the same
  invariants.
- *Comment composer keeps attachments separate* — files attached via
  the comment composer always become comment attachments, never bug-
  level. Bug-level attachments still have their own 📎 uploader.
- *Delete-attachment button no longer submits the bug form* — added
  the missing `type="button"` so a comment-attachment delete clicks
  cleanly without saving the bug or closing the modal.

## [2.8] — 2026-06-04

**Security hardening** — OWASP audit + remediation. Eight items, all
additive, no DB schema change.

- *Login timing equalised* (G1) — bcrypt runs even for unknown emails so
  response latency stops leaking account existence.
- *CSV export defanged* (G2) — cells starting with `=`/`+`/`-`/`@`/`\t`/`\r`
  prefixed with `'` to neutralise Excel formula injection.
- *Body-size middleware* (G3) — 60 MB cap (env-tunable via
  `MAX_REQUEST_BODY_BYTES`) rejects oversized requests before the body
  is read.
- *X-Forwarded-For trust gate* (G4) — `auth.py` now honours
  `TRUST_PROXY_FORWARDED_FOR` like the rest of the stack, so spoofed
  XFF can't poison the audit IP on direct deploys.
- *PII out of logs* (G5) — INFO log lines mask emails to `a***@domain`.
- *Per-account lockout* (T3) — 10 fails / 15 min triggers a 15-min 429.
  Email-keyed so unknown-email and known-email behave identically. Env
  tunables: `LOGIN_FAIL_LIMIT`, `LOGIN_FAIL_WINDOW_SECONDS`,
  `LOGIN_LOCKOUT_SECONDS`.
- *Breach-corpus check* (T4) — HaveIBeenPwned k-anonymity API rejects
  known-pwned passwords on every set-password path. Fail-open on
  network errors; off-switch via `PASSWORD_BREACH_CHECK_ENABLED=false`.
- *Image EXIF strip* (T6) — Pillow drops GPS / camera-serial / XMP /
  ICC from uploaded JPEG / PNG / GIF / WEBP / BMP / TIFF. Non-images
  pass through. +47 security tests (518 total).

## [2.7]

Quality, security, stability. SonarQube quality gate green (0 issues,
0 unreviewed hotspots, ~84% backend coverage). Cognitive complexity
refactored across 14 large functions (11 Python, 3 JS). 10 security
hotspots remediated in code rather than via UI review. Mechanical SPA
modernization (`Number.parseInt`, optional chaining, `replaceAll`,
`dataset`, `globalThis`). +66 new unit tests (471 total).
Accessibility polish (8 `aria-label` / role fixes). CSS contrast and
deduplication. Zero schema changes; production DBs byte-for-byte safe.

## [2.6]

Rich-text editor for descriptions and comments (B / I / U / lists /
blockquote / code / image paste-as-attachment), with Chrome-148
workaround that hand-rolls inline formatting in DOM code instead of
`execCommand`. Custom calendar / date-picker and custom dropdowns
replace browser-native widgets. Sidebar names are clickable to edit.
Newest-first comments / attachments / tasks. Audit log loads up to
5 000 rows with *Load older entries* button.

## [2.5]

Per-item-type status sets. Admin-curated comments and attachments.
Post-creation 📎 *Add attachment* button on the item detail. Global
blocking loader on every server action. Card-style controls bar on
Events / Sessions / Audit views.

## [2.4]

Audit history survives bug deletion (`activity_log.bug_id` becomes
`ON DELETE SET NULL` for fresh installs). Audit search LEFT-JOINs the
bugs table so live titles and types are searchable. Frontend-level
read-only mode for restricted users with a clear banner.

---

Older releases: see git history (`git log --oneline`).
