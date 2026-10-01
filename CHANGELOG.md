# Changelog

All notable changes to Bug Hunter. The format follows
[Keep a Changelog](https://keepachangelog.com/).

## [4.0] — Unreleased

Feature release: Bug Hunter gains opt-in Sprints & Agile, GitHub feature
branches for User Stories, bulk import, OpenTelemetry export, self-hosted API
docs and a JavaScript frontend. The name, logo and Steam-style theme are
unchanged. `init_db()` only adds tables, columns and indexes (plus one-time
backfills of the new display-ID and workflow-status columns), so an existing
database upgrades in place. See **Upgrading from 3.x** below.

### Added

- **Sprints & Agile (opt-in per project), modelled on Jira Scrum.** A ranked
  backlog with drag-and-drop sprint planning (multi-select, keyboard and menu
  moves), sprint create/edit/start/complete dialogs (completion keeps finished
  issues with the closed sprint and moves the rest to the backlog, a future
  sprint or a new sprint), the active sprint board (columns mapped to statuses,
  column min/max limits, swimlanes, quick filters, flags), an Epics view
  (Epic → issue → Sub-task with progress), board settings (columns and
  statuses, estimation statistic, working days, time zone, quick filters),
  capacity planning, releases/components/labels, and reports: burndown,
  burnup, sprint report, velocity, cumulative flow, control chart, Epic
  report, daily summary and workload, each with CSV export. The hierarchy is
  Jira's three levels (Epic; Story, Task, Bug, Requirement; Sub-task), enforced
  server-side on every write path. Sub-tasks follow their parent's sprint and
  Epic, Epics never sit in a sprint, and "done" means the board's right-most
  column. Every change to status, sprint, estimate, Epic, parent, type or
  project goes into a change log, and the reports replay it, so commitment,
  scope change and re-estimates match what Jira would show. Lifecycle and bulk
  endpoints accept an `Idempotency-Key` header.
- **GitHub feature branches.** Per-project Git settings with a Fernet-encrypted
  token (`GIT_CREDENTIAL_ENCRYPTION_KEY`), connection test and read-only
  repository discovery. A User Story can create a deterministic
  `feature_<id>_<slug>` branch in a chosen repository and, when
  `GIT_BRANCH_DELETION_ENABLED=true`, remove exactly that branch again.
- **Bulk import** of work items from an Excel template or CSV, validated row by
  row with per-row errors.
- **OpenTelemetry** traces, metrics and logs to any OTLP/gRPC collector (such as
  SigNoz) via `OTEL_EXPORTER_OTLP_ENDPOINT`, and `LOG_FORMAT=json` for log
  shippers.
- **Self-hosted, CSP-safe Swagger UI and ReDoc** at `/docs` and `/redoc`.
- **Sleuth tool calling** (cloud layer): create work items under a parent,
  transition them, and manage sprint membership, all with confirmation and
  under the caller's own permissions.
- **Frontend tooling:** Vitest behaviour tests and ESLint (`npm test`,
  `npm run lint`), which CI runs as blocking jobs.
- **Release hygiene:** `APP_VERSION` in `.env` is the single source of the
  product version (UI, API docs, `/api/health`, image tag).
- **One-command local run:** `scripts/run_local.ps1` (Windows) and
  `scripts/run_local.sh` create `.venv` and `.env` on first run and start the
  app on <http://127.0.0.1:8000>.
- **Notification links work.** Emails, push and desktop notifications have
  always linked to `/#bug=<id>` or `/#event=<id>`, but the app ignored the
  link. It now opens the item or event, including after signing in first.
- **Browser end-to-end suite** (`tests/test_e2e_browser.py`): real-server
  workflows across Chromium, Firefox and WebKit, plus axe-core WCAG 2.1 AA
  checks of every view in both themes.
- **Production-readiness audit** in `audit/` (findings, test, security and
  performance reports, environment reference, known limitations) and
  `HUMAN_ACTIONS.md` for the steps that need an operator.

### Changed

- **Sprints redesigned before release to follow Jira Scrum.** The preview's
  six-level hierarchy (Collection → Epic → Feature → User Story → Task →
  Sub-task) had two kinds of task and did not keep parent/child rules. It is
  now Jira's three levels. Collections and Features are retired: on first
  boot they become labels on the issues they grouped, a Story under a Feature
  joins that Feature's Epic, sub-tasks without a valid parent become Tasks,
  and Epics leave sprints (`app/agile/upgrade.py`, idempotent, verified on
  SQLite and PostgreSQL 16). The Sprints tabs are now Backlog, Active sprint,
  Epics, Reports, Releases & labels, Capacity and Board settings. The reports
  were rewritten to replay a per-field change log instead of sprint
  membership snapshots, so commitment, added/removed scope, re-estimates,
  reopened issues and the non-working-day guideline are counted as Jira does.
- The frontend is now plain JavaScript (JSX). TypeScript and `tsconfig.json`
  are gone, and the Docker image compiles the bundle in a `node:20-slim` stage.
- The logger namespace is `bug_hunter.*` and the default local SQLite file is
  `bug_hunter.db`.
- On PostgreSQL, `init_db()` takes an advisory lock so concurrent boots can't
  race on schema changes.
- Production start-up checks are two-tier. Any production deploy
  (`COOKIE_SECURE=true` or `APP_ENV=production`) needs a real session secret, a
  non-placeholder bootstrap password, auto-login off and a valid Git encryption
  key. `APP_ENV=production` also needs secure cookies, an https
  `APP_BASE_URL` and a real email backend. All failures are reported at once.
- Item descriptions are plain text (the xOPS design): the description editor
  no longer shows a formatting toolbar whose effects were discarded on save.
  Comments keep rich text. Descriptions saved as HTML by 3.x display as plain
  text and are stored as plain text the next time the item is saved.
- Timestamps in API responses always carry a UTC offset. On SQLite they had
  none, so browsers read them as local time and shifted every displayed time.
- CI runs on `main`; the SonarQube scan and image push are opt-in through the
  `SONAR_ENABLED` and `IMAGE_PUSH_ENABLED` repository variables; all actions
  are pinned to commit SHAs.
- The container runs as a fixed numeric user (UID/GID 1000, as before) so
  `runAsNonRoot` policies can verify it.
- Frontend tooling: Vite 6 and Vitest 4; the unused `happy-dom` package is
  removed (npm audit: 0 vulnerabilities, was 6).
- The unused `API_KEY` setting is removed: it was passed through Compose but
  never read, so setting it gave a false impression of API-key protection.
- `SESSION_REQUIRE_JTI` and `TRUST_PROXY_HOP_COUNT` are now documented in
  `.env.example`.

### Fixed

- Independent audit of the Sprints redesign (backend, reports, frontend,
  tests), every finding fixed and covered by a test:
  - Backlog ranks could run out after a few hundred issues (duplicate ranks,
    "not next to each other" errors). Ranks now stay short, and a list is
    re-spaced automatically when a gap is used up.
  - Members could change a sprint's scope through the item dialog or the API.
    Planning work into or out of a sprint now needs the manager's backlog
    permission everywhere, as on the board.
  - An issue reopened after its sprint closed disappeared from every list. It
    now returns to the backlog, with its Sub-tasks.
  - Bulk delete left orphan Sub-tasks, and deleting an Epic left its issues
    pointing at it.
  - Two simultaneous sprint starts (or completes) could both succeed on
    PostgreSQL. Lifecycle requests now take a row lock.
  - Reports: issues completed before they joined a sprint counted as
    completed and in velocity; the cumulative-flow CSV exported the column
    list and the sprint CSV only one outcome; the control chart showed a dot
    per completion instead of per issue; the daily summary ignored same-day
    reopens; closed-sprint workload used current membership; burndown
    weekends were shaded in the viewer's timezone instead of the board's.
  - Task followed the Sub-task workflow; labels and components could be
    attached from another project; capacity counted Sub-tasks.
  - New issues defaulted into the active sprint (Jira creates them in the
    backlog); the Sub-task parent picker listed only 200 issues (now a
    search); board settings lost unsaved edits on refresh; capacity could be
    saved into the wrong sprint; a partly failed save could not be retried.
  - Accessibility: buttons nested inside draggable cards and rows, dialogs
    that were neither announced nor focused, unlabelled capacity inputs.
    Every Sprints tab now passes axe (WCAG 2.1 AA) in both themes. Backlog
    rows have a drag handle for keyboard ranking; touch devices drag with a
    long press so swiping still scrolls.
- A second independent audit round verified those fixes and found more,
  all fixed and tested:
  - **The app would not start on an existing database with long backlogs.**
    The earlier rank algorithm had left tokens of up to 62 characters; the
    first boot now re-spaces any damaged list in its current order (verified
    on a 31,000-issue production-mode database), and appending never fails.
  - Moving an issue to another project took it out of a running sprint
    without the backlog permission, and kept the old project's labels and
    components.
  - Issues in a closed sprint could still be re-ranked.
  - Planning an issue into a sprint that another request was completing at
    the same moment could leave unfinished work in the closed sprint. Every
    sprint change now locks the sprints involved.
  - Sleuth started sprints with the server's date instead of the board's.
  - The item dialog could carry a sprint choice into the next item; an open
    ⋯ menu did not close when another one was opened; archived Epics were
    offered as move targets; burndown day labels and weekend shading could
    spill outside the chart.
- Sprints drag and drop dropped issues in the wrong place: the drag preview
  was drawn offset by the sidebar and header (the page's entry animation left
  a transform on the view), and collision used that preview, so backlog drops
  landed on the wrong row and board drops in the neighbouring column. The
  preview now renders at the pointer, and a board card lands in the column
  under the pointer. Cards and backlog rows can also be dragged by their
  title, and dropping never opens the issue by accident.
Bugs found in the xOPS Tracker code while porting its features:

- The app crashed at import when `APP_VERSION` was blank, because FastAPI
  rejects an empty version. It now reports `dev`.
- API docs were always on, including in production. The `ENABLE_API_DOCS` gate
  is restored.
- The strict production checks were unreachable (a dead `is_production_env`
  lookup), and a `COOKIE_SECURE`-only deploy was held to the full production
  bar. The checks are now the two tiers above, and the Compose file no longer
  forces `APP_ENV=production`.
- Production accepted the `.env.example` placeholder session secret and
  bootstrap password.
- Agile endpoints (board drag, estimate, flags, releases/labels, hierarchy,
  feature link, ready flag, dates, task flags, acceptance criteria) skipped the
  work-item edit policy, so regular users could change Tasks and Requirements
  that `PUT /api/bugs` refuses them.
- Moving a legacy Task on the board silently rewrote it to a Sub-task, which
  dropped it from the Tasks tab and the backlog.
- Collection and Feature cards could be transitioned without a version check
  and stored raw column names as their status. They now map to the hierarchy
  statuses with optimistic locking.
- A hierarchy card could appear in two board columns, Epics could appear
  twice, and private collections were shown to non-members.
- WIP limits counted the whole project (backlog and other sprints included)
  instead of the board's sprint, so they fired on nearly empty columns.
- Changing an item's sprint or status in the item dialog or through bulk
  actions skipped the backlog rank and the sprint history, so the item vanished
  from Sprint Items and reports missed the change. Moving an item to another
  project now checks the sprint against the target project and refuses to
  orphan children.
- Creating an agile item with a sprint bypassed the sprint service (no rank or
  history), ignored the chosen status and never notified assignees. Epics now
  join sprints through their epic record.
- Collections, features and epics couldn't clear a sprint, date or owner once
  set, and several release, component, label and collection writes weren't
  audited. Private collections could be read by non-members.
- Agile report date ranges weren't validated (inverted or unbounded ranges),
  CSV exports were open to spreadsheet formula injection, and exports ignored
  the export permission.
- Sprint capacity accepted duplicate or unknown users and wasn't audited.
- Git repository discovery and metadata verification used different page
  limits.
- Bulk import accepted titles over the 200-character limit and stored
  descriptions as raw rich text, and a database error aborted the whole import
  with a 500.
- Sleuth: confirmed writes weren't audited, agile items could be created in
  projects without Agile, invalid parents raised an internal error, created
  bugs had no backlog rank, and the assign and comment tools sent no
  notifications. Garbled em dashes in tool messages are fixed.
- Frontend: board drag IDs collided between work items and
  Collections/Features, so dragging one could move the other, and clicking a
  Collection or Feature card opened an unrelated bug. Creating a Collection or
  Feature with a sprint added the wrong item to the sprint, agile creates
  dropped assignees and status, Sub-tasks had no parent picker, and Features
  were silently attached to the first Epic. Sprint end dates and "today" were
  computed in UTC and could be off by a day. The hierarchy view offered delete
  where only archive applies and listed archived items. Leftover TypeScript
  syntax was removed from 25 files.
- Tests contained realistic-looking database credentials. They now use a
  placeholder host.
- `down.sh` removed a different image name than the one Compose built.
- `scripts/gen_local_env_secrets.py` overwrote real secrets on every run
  (re-running it after the first Docker boot changed `POSTGRES_PASSWORD` and
  locked the app out of its database). It also missed the `replace_with_…`
  placeholders, and it could generate passwords with `$`, `#`, `@` or `%`,
  which break Compose interpolation and the database URL. It now keeps real
  values, recognises the placeholders, uses only safe characters, creates
  `.env` from the template when missing, and generates the Git encryption key.
- Release archives from `scripts/make_release.py` left out `DEPLOYMENT.md` and
  `CHANGELOG.md`.
- The README and CONTRIBUTING told Windows and macOS users to install from the
  Linux-only hashed lockfile, which fails there. The lock-regeneration command
  didn't match how the lockfiles are actually built.

Found in the production-readiness pass (see `audit/AUDIT_REPORT.md`):

- **Connection-pool deadlock under load.** A burst of concurrent requests
  could exhaust the database pool while every worker thread waited for a
  connection, stalling all requests until the 30-second pool timeout (98 of
  ~100 requests failed in a 100-user spike). Sessions are now admitted on the
  event loop up to the pool's capacity; the same spike completes with zero
  errors.
- **Startup race between replicas.** On an empty PostgreSQL database a second
  replica seeded the first admin and backfilled boards before the tables
  existed (or alongside the first replica). All startup writes now run once,
  under the migration lock.
- **Open redirect after login.** `?next=/%09/evil.example` passed the
  same-site check (browsers drop the tab) and sent users off-site after
  signing in. The target is now resolved and must be same-origin.
- **HTTP 500 on out-of-range ids.** Ids wider than 64 bits in any path, query
  or body crashed with `OverflowError`; they now return 422.
- **HTTP 500 on duplicate sprint names.** Creating or renaming a sprint to an
  existing name crashed instead of returning 409.
- A daily sprint report for an impossible date (`0000-00-00`) returned a
  Python error message; it now returns a clean 422.
- Web push retried for about 6.5 minutes when the Firebase SDK was blocked on
  the page, although a retry can't help; it now stops at once.
- Accessibility: event names rendered black on the dark theme (an unstyled
  `<button>`), and several labels, timestamps and badges failed WCAG AA
  contrast. Four dropdowns and the rich-text editors had no accessible name.
  All nine pages now pass axe-core WCAG 2.1 AA (serious and critical) in both
  themes.
- Two frontend test files had tests nested inside other tests, so 15 tests
  had silently never run; they run (and pass) now.
- Excel, CSV and attachment downloads were documented in the API schema as
  JSON.
- Dead code: five unused permission helpers and an unused auth dependency in
  `app/auth.py` removed; about 170 comment references to an unpublished
  planning document replaced with plain explanations.

Email digest fixes:

- **Email digest fired at the wrong local time.** The slim Docker image ships no
  IANA timezone data, so `EMAIL_DIGEST_TIMEZONE` silently fell back to UTC and a
  `0 8 * * *` schedule ran at 08:00 UTC instead of 08:00 local. The `tzdata`
  package is now a pinned dependency; rebuild the image (`./deploy.sh`) to pick
  it up.
- **A failed digest send lost that day's emails.** Rows were stamped as emailed
  before the SMTP send; a delivery failure was ignored. Failed sends are now
  released and retried on the next scheduled run, with an error in the log.
  At-most-once delivery is preserved.

Email digest changes:

- `EMAIL_DIGEST_LOOKBACK_HOURS` default raised from 26 to 50 (twice the daily
  gap), so one missed or failed run is fully caught up by the next. A wider
  window can never double-send.
- The startup log now warns loudly when digest mode is enabled without a
  schedule (`EMAIL_DIGEST_CRON` empty means no work-item email is ever sent),
  and when the configured timezone cannot be loaded.
- Comments across the codebase trimmed to a concise one-line style; no
  behavioral changes.

### Upgrading from 3.x

1. Add `APP_VERSION=<release>` to `.env`. Compose refuses to start without it.
2. Make sure `POSTGRES_PASSWORD` and `BOOTSTRAP_ADMIN_PASSWORD` are set in
   `.env`, keeping the database password your volume was created with.
3. Run `./deploy.sh`. New tables and columns are added on first boot.
4. Agile stays off until a manager or admin enables it per project with
   **Sprints → Set up Scrum board**.
5. Item descriptions become plain text (see Changed). Stored 3.x HTML is left
   untouched until an item is next saved; take a database backup first if you
   want to keep the original formatting.
6. A new index (`idx_bugs_project_updated_id`) is created on first boot; on
   large databases the first start takes a little longer.

## [3.1] — 2026-06-29

Hardening release for the **Sleuth** AI assistant, following an independent
audit. `init_db()` only adds new tables and columns; it never modifies existing
rows, so a production database is left intact.

### Sleuth AI assistant

- **Project-scoped answers.** The cloud assistant's free-form data path and
  retrieval are now scoped to the projects the requesting user can access.
  Managers and regular users can no longer read bugs, statistics, or reports
  from projects they aren't assigned to. The write firewall is unchanged; the
  cloud layer still cannot perform any write.
- **Non-repetitive replies.** Conversational answers use mild sampling
  (temperature plus frequency/presence penalties). Routing, the LLM judge, the
  read-only agent, and ingestion remain fully deterministic. Greetings, thanks,
  and help requests resolve from rules without reaching the model.
- **New guardrails.** Added a prompt-injection/instruction-extraction boundary,
  a flag for hallucinated write claims ("I closed / assigned ..."), an app-side
  answer-length ceiling, control-character scrubbing, a fenced-and-defanged
  judge prompt, and a canonical-query length cap.
- **Evaluation harness.** An offline, network-free harness covering six
  standards: LLM-as-judge grounding, agent trajectory, outcome, confidence
  calibration (Brier score), reliability (answer variance/route stability), and
  pass@k. Backed by in-process observability counters (provider, route, judge
  outcomes, cooldown trips).

## [3.0] — 2026-06-13

A self-hosted tracker for bugs, requirements, and tasks, built on FastAPI +
PostgreSQL with a React + TypeScript frontend. `init_db()` creates new tables
and columns on boot without touching existing rows, so a production database is
left intact.

### Work items

- Bugs, requirements, and tasks share one `#N` counter. A tab strip filters
  KPIs, columns, and analytics by type. Each type has its own statuses; bugs
  also carry a DEV/UAT/PROD environment.
- Items can be linked (relates, blocks, duplicate). Adding or removing a link
  requires edit rights on both items and is safe under concurrent requests.
- Rich-text descriptions and comments (bold, italic, lists, code, quotes). PDF,
  image, and video files are stored in PostgreSQL. Pasted images become real
  attachments; image metadata (EXIF) is stripped.
- Bulk status, priority, environment, and delete actions across multiple items,
  each row checked for concurrent edits.

### Projects, events, and reporting

- Projects group work; events group items for a standup or sprint and have one
  or more managers.
- Managers and admins can export a multi-sheet Excel report, with a row limit
  per export.
- In-app notification bell with a live unread badge, email (per event or a
  daily digest), and optional browser push via Firebase Cloud Messaging. The
  push table is keyed on the token plus a platform column, so a native Android
  client uses the same send path.
- Audit log of every create, update, delete, and login, readable by admins and
  managers. Entries persist after an item is deleted.
- Admins can see every active session (user, role, IP, browser, time) and log
  out a single device.

### Login and roles

- Local login with bcrypt-hashed passwords and three roles: admin, manager, and
  user. Role checks apply the same way on the REST and chat write paths.
- Server-side sessions, signed HttpOnly SameSite cookies (Secure over HTTPS),
  per-account lockout, evened-out login timing to prevent email enumeration, and
  an optional HaveIBeenPwned check on password set.
- Password reset that doesn't reveal whether an email exists, using single-use,
  hashed, expiring tokens.

### Sleuth assistant

An in-app assistant that answers plain-English questions and runs audited
actions. It tries four layers in order and is fully local by default:

1. A rule-based parser over verbs, filters, names, and IDs.
2. A TF-IDF / cosine-similarity classifier over a curated corpus, with no
   external model files.
3. An optional, lazily loaded local LLM (llama.cpp against a GGUF model).
4. An optional cloud LLM (Groq or OpenRouter), off by default.

Read intents only `SELECT`. Write intents go through the same audited paths as
the REST API, are confirmed before any change, and are re-checked against the
write policy at confirm time. When the cloud layer is on, all outbound text
passes through a secret-redaction filter first. Four read-only accuracy add-ons
(grounding, a multi-step agent, citation verification, and answer evaluation)
can each be turned on separately.

### Security

- Content-Security-Policy (`script-src 'self'`, no CDN) and a full set of
  security headers, applied to error responses (`429`, `403`, `500`) too.
- CSRF Origin/Referer checks on state-changing requests, including login.
- Per-IP and per-account rate limits on login, password reset,
  change-password, commenting, and chat.
- Rich text is sanitized server-side against an allowlist, then again in the
  browser with DOMPurify before display.
- Guards against mass-assignment on item-type changes, length caps on request
  lists and search terms, a global request body-size cap, and formula-injection
  defense on Excel export.
- Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are served only
  outside production.

### Reliability and performance

- Bug and bulk edits use optimistic concurrency: a stale save returns `409`
  instead of overwriting another user's edit.
- If the database is down at boot, the app serves a degraded state
  (`/api/health` reports `status: degraded`) instead of crash-looping.
- Oversized or decompression-bomb images are skipped by a pixel budget and fall
  back to the original bytes.
- The frontend code-splits secondary views, the assistant panel, and the
  rich-text editor to load them on demand. Idle data and session polls skip
  re-rendering when nothing changed. Reports and list endpoints are row-limited
  server-side; indexes keep audit-trail and digest queries fast.

### Notes

- The frontend is built with Vite into `app/static`, which FastAPI serves
  directly.
- Secrets (`.env`, `secrets/firebase-admin.json`) are gitignored and set up per
  server.
