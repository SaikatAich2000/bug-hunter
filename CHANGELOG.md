# Changelog

All notable changes to Bug Hunter. The format roughly follows
[Keep a Changelog](https://keepachangelog.com/). Releases older than 2.4 live
in git history only.

## [3.0] — 2026-06-13

Major release: new frontend, in-app notifications, and a UX overhaul. Database
changes are strictly additive (one new `notifications` table created by
`init_db()` on boot); no existing table is altered, so the production database
is untouched on upgrade.

### Added

- **React 18 + TypeScript frontend.** The UI is rebuilt as a Vite SPA that
  builds into `app/static`; the backend HTML-serving contract is unchanged.
- **Per-user in-app notifications.** A top-bar bell with a live unread badge
  surfaces the events you already receive emails for (assignment, comments on
  items you report or are assigned to, status and field changes). Strictly
  per-user and role-respecting. New `/api/notifications` API: list, unread
  count, mark-read, read-all, delete.
- **Optional daily email digest.** Set `EMAIL_DIGEST_ENABLED=true` to batch each
  user's per-operation emails (new item, update, assignment, comment, event)
  into one grouped email per day, in five sections. The standalone job
  (`python -m app.jobs.email_digest`) is idempotent (it stamps each operation
  via the additive nullable `notifications.emailed_at` column) and
  window-bounded (`EMAIL_DIGEST_LOOKBACK_HOURS`, default 26h). Security and
  password-reset emails are never batched. An optional in-app scheduler
  (`EMAIL_DIGEST_CRON` / `EMAIL_DIGEST_TIMEZONE`) removes the need for host cron.
- **Web push (Firebase Cloud Messaging), optional and off by default.** Browser
  push for the five operations, sent immediately. The new `push_subscriptions`
  table is keyed on the FCM registration token plus a `platform` column, so a
  native Android client registers into the same table and send path. The
  Firebase SDK is vendored locally (CSP stays `script-src 'self'`), and
  `connect-src` relaxes for FCM endpoints only while push is enabled.

### Changed

- **Visual identity (light + dark).** A five-color system derived from the brand
  logo, applied across KPIs, status/priority/environment badges, buttons, and
  menus, plus self-hosted Plus Jakarta Sans (UI) and JetBrains Mono (code).
  Design-token names and DOM hooks are unchanged; only token values changed.
- **Shell redesign.** The top bar ends in a single right-hand cluster: New, the
  notifications bell, and a profile menu (account, change password, theme
  toggle, log out). The sidebar is now navigation and project/people lists only.
- **Sleuth improvements.** Understands bare (unquoted) free-text search after a
  topic cue, with a guard so a pure filter phrase is not mistaken for a topic;
  the unknown-intent fallback offers a classifier-ranked "did you mean"
  suggestion. Both are rule-based or statistical — no network or LLM required.
- **Login version line** moved below the sign-in card; the Sleuth panel is now
  resizable with persisted size.
- Roles and permissions are unchanged from 2.10.

### Security

- IP rate limits extended to `reset-password` and `change-password` (the latter
  runs a bcrypt verify on every call). Per-user rate limit on commenting. The
  password-reset "no account" audit line now masks the email like every other
  auth log. `assignee_ids` / `manager_ids` request lists are length-capped. The
  logout cookie is cleared with the same flags it was set with.

### Performance

- Two new `activity_log` indexes (`(action, bug_id)` for throughput reports,
  `actor_user_id` for the audit "filter by actor" screen), created the additive
  way. The dashboard timeline filter is now sargable. The audit trail pages in
  300-row chunks instead of loading 5000 rows up front.

### Post-release hardening — 2026-06-15

Security and performance hardening plus a front-end auto-refresh, responsive,
and contrast pass. Code/config only or additive DB (two new indexes via
`init_db()`); production data is untouched and the legacy `changeme` password
stays valid. SonarQube: 0 open issues, A/A/A ratings; 747 backend tests pass at
91% coverage.

- **Security.** Sleuth chat enforces the same per-type edit rules as the REST
  API, so a regular user cannot edit tasks/requirements via chat that
  `PUT /api/bugs/{id}` forbids. `PUT /api/bugs/{id}` re-authorizes an
  `item_type` change against the target type (mass-assignment guard).
  `POST /api/auth/forgot-password` is account-enumeration-safe by default
  (always 204; `FORGOT_PASSWORD_ENUMERATION_SAFE=false` restores the legacy
  404). `/api/events`, `/api/users`, `/api/projects`, and the Reports XLSX
  export are row-bounded (and `users?q=` is length-capped). The SPA refuses to
  mount a privileged view (Reports/Audit/Sessions) — and fire its fetch — for
  under-privileged users. New covering indexes on
  `password_reset_tokens(user_id, used_at)` and `event_managers(user_id)`.
- **Performance.** Front-end code-splitting drops the main bundle from ~137 kB
  to ~77 kB (secondary views, the Sleuth panel, and the rich-text editor load on
  demand). The idle 10s data poll and 15s session poll skip the re-render when
  nothing changed; FilterBar/MsFilter and audit rows are memoized; Reports
  detail queries are row-bounded server-side.
- **Auto-refresh.** Users/projects, the open bug modal, Events, Sessions, Audit,
  and the notifications list poll on a shared 10s cadence (paused when the tab is
  hidden, instant on refocus).
- **Contrast and responsiveness.** Light-mode contrast raised to WCAG AA (KPI
  numerals, status/priority/environment tokens, muted text, borders); dark theme
  unchanged. Phone-responsive fixes across Audit, Sessions, Events, the KPI
  grid, and the Reports filter, with 44 px touch targets.
- **New flags:** `FORGOT_PASSWORD_ENUMERATION_SAFE`, `MAX_REPORT_ROWS`,
  `PASSWORD_MIN_LENGTH`, `PASSWORD_REQUIRE_COMPLEXITY` (the legacy `changeme` is
  always accepted), plus the in-app digest scheduler.
- **SonarQube.** Fixed every open finding in the Sleuth code (logger-name
  constant, reduced cognitive complexity in `rag.index_all`, removed unused
  parameters, documented HTTP exceptions, `Annotated` dependencies, explicit
  `BugOut.due_date` default). Pruned 25 stale Vite bundles from
  `app/static/assets` and excluded that generated output from the scan; the
  source lives in `frontend/`.

## [2.10] — 2026-06-11

Maintenance and quality pass. No DB schema changes.

- SonarQube cleanup: resolved all open issues and security hotspots.
- Expanded automated test coverage.

## [2.9] — 2026-06-09

Reports view and Sleuth report intent. No DB schema change — the "who resolved
this item and when" attribution is derived from the existing `activity_log`
table, so the production database is untouched on upgrade.

- **Reports view** — a report builder behind a manager/admin role gate, with
  nine report types (Item Detail Export, Resolution Throughput, Pending Items
  Snapshot, Status Distribution, Priority Distribution, Project Breakdown,
  Aging, Created-vs-Resolved Timeline, Time to Resolution) and a universal filter
  set (date range, item types, statuses, priorities, environments, projects,
  assignees, reporters, free text).
- **Excel export** — multi-sheet XLSX with the aggregated view, a drill-down
  Items sheet, and a Filters Applied sheet. Formula-injection defense moved from
  the retired CSV export to the XLSX writer.
- **Sleuth report intent** — natural-language report queries reuse the same
  engine and produce an inline preview plus a downloadable spreadsheet.
- **Legacy CSV export retired** — `/api/bugs/export.csv` removed; its tests moved
  to the XLSX path with the same invariants.
- **Comment composer fixes** — files attached via the comment composer always
  become comment attachments; the delete-attachment button no longer submits the
  bug form.

## [2.8] — 2026-06-04

Security hardening from an OWASP audit. Eight items, all additive, no DB schema
change.

- Login timing equalized — bcrypt runs even for unknown emails so latency no
  longer leaks account existence.
- CSV export defanged — cells starting with `=` `+` `-` `@` `\t` `\r` are
  prefixed with `'` to neutralize spreadsheet formula injection.
- Body-size middleware — a 60 MB cap (`MAX_REQUEST_BODY_BYTES`) rejects oversized
  requests before the body is read.
- X-Forwarded-For trust gate — `auth.py` honors `TRUST_PROXY_FORWARDED_FOR`, so
  spoofed XFF cannot poison the audit IP on direct deploys.
- PII out of logs — INFO log lines mask emails to `a***@domain`.
- Per-account lockout — 10 failures in 15 minutes triggers a 15-minute 429,
  email-keyed so known and unknown emails behave identically
  (`LOGIN_FAIL_LIMIT`, `LOGIN_FAIL_WINDOW_SECONDS`, `LOGIN_LOCKOUT_SECONDS`).
- Breach-corpus check — the HaveIBeenPwned k-anonymity API rejects known-pwned
  passwords on every set-password path (fail-open on network errors; off-switch
  via `PASSWORD_BREACH_CHECK_ENABLED=false`).
- Image EXIF strip — Pillow drops GPS, camera-serial, XMP, and ICC metadata from
  uploaded images. Non-images pass through. +47 security tests (518 total).

## [2.7]

Quality, security, and stability. SonarQube quality gate green (0 issues, 0
unreviewed hotspots, ~84% backend coverage). Cognitive complexity refactored
across 14 functions. 10 security hotspots remediated in code. SPA
modernization. +66 unit tests (471 total). Accessibility and CSS-contrast
polish. Zero schema changes.

## [2.6]

Rich-text editor for descriptions and comments (bold/italic/underline, lists,
blockquote, code, image paste-as-attachment), with a Chrome-148 workaround that
hand-rolls inline formatting instead of using `execCommand`. Custom calendar and
dropdown widgets replace browser-native ones. Sidebar names are clickable to
edit. Comments, attachments, and tasks are newest-first. The audit log loads up
to 5000 rows with a "Load older entries" button.

## [2.5]

Per-item-type status sets. Admin-curated comments and attachments. A
post-creation "Add attachment" button on the item detail. A global blocking
loader on every server action. Card-style controls bar on the Events, Sessions,
and Audit views.

## [2.4]

Audit history survives bug deletion (`activity_log.bug_id` becomes
`ON DELETE SET NULL` for fresh installs). Audit search left-joins the bugs table
so live titles and types are searchable. Frontend read-only mode for restricted
users, with a clear banner.

---

Older releases: see git history (`git log --oneline`).
