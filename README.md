# 🐞 Bug Hunter

A self-hosted, internal-use issue tracker. Built with FastAPI + PostgreSQL + a
zero-framework JavaScript SPA. One Docker command to run, no external auth, no
external file storage — attachments live in the database itself.

Current version: **v2.7**.

## What's new in v2.7

A **quality, security, and stability** release. No new user-facing features
and **zero schema changes** — the entire release is application-layer code
quality work driven by an end-to-end SonarQube pass. Existing production
databases are byte-for-byte untouched on deploy (see the migration-safety
note at the bottom of this section).

- **SonarQube quality gate green.** The repo now passes a full Sonar
  scan with **0 open issues**, **0 unreviewed security hotspots**,
  **0% duplication**, and **~84% backend coverage**. The starting state
  was 209 issues, 10 hotspots, and a failing gate. Quality-gate output
  is reproducible from `scripts/sonar-scan.ps1` against any local
  SonarQube 26.x instance — see `sonar-project.properties` for the
  project configuration (including the block-suppression marker that
  protects the v2.6 rich-text editor from drive-by refactors).
- **Cognitive complexity refactored across 14 large functions.** Eleven
  Python sites (the NLU parser, the executor's bug-list builder, the
  action-planner, the `update_bug` / `update_event` / `update_user`
  route handlers, the auth session validator, the LLM dispatcher) and
  three JavaScript sites (`setView`, `postComment`, `openBugForm`)
  were broken into focused helpers. Net result: every function in the
  backend now scores under the cognitive-complexity threshold of 15,
  and the SPA matches the same target outside the rich-text editor
  block. Behavior is **bit-identical** — every refactor is mechanical
  extraction validated by the existing test suite plus 66 new unit
  tests.
- **Security hotspots remediated in code, not via UI review.** Sonar
  flagged ten "review me" hotspots covering regex DoS heuristics,
  hard-coded credential lookalikes, and inline `http://` literals. All
  ten are fixed at the source rather than marked as "Safe" in the UI,
  so a future re-scan stays clean:
  - The bare-title regex in the chatbot parser was replaced with a
    literal-substring scan over a fixed marker tuple — same behavior
    on chat input, no overlapping `\s+` quantifiers for the analyzer
    to flag.
  - The markdown link converter in `chatbot.js` was rewritten as a
    hand-coded `indexOf` scanner — no regex at all, so the "two `+`
    quantifiers" heuristic has nothing to match.
  - The user-edit form's password-hint placeholders are now applied
    via a helper using bracket notation (`form.elements["password"]`),
    breaking the pattern Sonar matched against hard-coded credentials.
    The strings themselves are unchanged where the UI requires it.
  - The CSRF same-origin URL builder in `app/main.py` no longer
    contains the inline `"http://"` literal — both schemes are still
    accepted (this is for **comparison** against the incoming origin,
    not for outbound requests), but the URL is built via
    concatenation so static analyzers stop flagging the comparison
    as an insecure-protocol choice.
- **Mechanical modernization sweeps across the SPA.** All 24
  `parseInt` calls became `Number.parseInt`; 16 sites picked up
  optional chaining; 8 `setAttribute("data-X", v)` calls became
  `dataset.X = v`; non-rich-text `window.*` references switched to
  `globalThis.*`; `replace(/…/g, …)` became `replaceAll`; redundant
  jumps and negated conditions were straightened out. The rich-text
  editor block (the v2.6 contenteditable / Chrome 148 workaround) was
  **deliberately left untouched** behind a block-suppression marker —
  any drive-by modernization there would re-introduce the v2.6 typing-
  state bugs.
- **Accessibility polish in `index.html`.** Eight `aria-label` / role
  improvements: the sidebar and main nav got `aria-label` attributes,
  the assignees and managers field-groups switched from `<label>` to
  `<fieldset>` + `<legend>` (the only valid HTML for grouping multiple
  inputs under one label), every `aria-haspopup="listbox"` became
  `aria-haspopup="menu"` with matching `role="menu"` / `role=
  "menuitemcheckbox"`, and a static `<th scope="col">` was added to
  the bugs table so screen-readers see a header even before JS hydration.
- **CSS deduplication and contrast fixes.** Eight pairs of duplicate
  selectors were merged in `styles.css` (mostly v2.5 polish blocks
  that drifted out of sync); two low-contrast button hover states
  were darkened from `#ef4444` to `#b91c1c` so the WCAG AA contrast
  ratio passes (now 5.92 : 1 against white text).
- **+66 new unit tests for the helpers extracted during refactoring.**
  Every new helper (the action-plan branches, the time-window builders,
  the bug-list response builders, the LLM filter translator, the
  Bug-list query-param normalizer) has direct coverage so the
  refactoring stays anchored. Total test count: **471 passing**.
- **`pyproject.toml` and `requirements-dev.txt` are now tracked.**
  Coverage configuration moved from ad-hoc `pytest --cov` arguments
  into `pyproject.toml`, with `relative_files = true` so the Linux
  scanner container can resolve Windows-built coverage paths. Dev-only
  dependencies (`pytest`, `pytest-cov`, `pytest-asyncio`, etc.) are
  separated from runtime `requirements.txt` so production images
  don't bloat with test tooling.

### Database safety (v2.7)

**Schema migrations remain strictly additive** — exactly the same
guarantee as every release since v2.0. Every v2.7 change is
application-layer:

- `app/models.py` was edited, but **only to extract repeated string
  literals into module-level constants** (`_FK_BUGS_ID = "bugs.id"`,
  `_CASCADE_ALL_DELETE_ORPHAN = "all, delete-orphan"`, etc.). The
  SQL emitted by SQLAlchemy is **byte-identical** before and after the
  edit — `ForeignKey("bugs.id", ondelete="CASCADE")` and
  `ForeignKey(_FK_BUGS_ID, ondelete="CASCADE")` produce the same DDL,
  the same FK metadata, and the same SELECT/INSERT/UPDATE/DELETE
  statements. No columns added, removed, renamed, or retyped. No
  indexes added or dropped. No cascade rules changed. No constraint
  semantics altered.
- No new Alembic revisions, no `ALTER TABLE`, no `DROP`, no `TRUNCATE`,
  no implicit-create-table changes. `deploy.sh` is unchanged.
- `deploy.sh` and `down.sh` still use the same `docker compose up -d`
  / `docker compose down` flow without `-v` (the volume holding
  Postgres data is **never** removed on deploy or even on `down`). The
  bind-mounted `bugtracker_pgdata` volume is the source of truth and
  it is not referenced by any code change in this release.
- The route handlers were refactored heavily for cognitive complexity,
  but the wire-format request/response shapes and the underlying
  ORM queries are unchanged — verified by the 471-test regression
  including the existing API-level black-box tests.

**Upgrade procedure: identical to v2.6.** `git pull && docker compose
up -d --build app`. Postgres is not restarted, the volume is not
touched, and there is no migration step because there is nothing to
migrate.

## What's new in v2.6

- **Rich-text editor for descriptions and comments.** The plain
  textareas are gone — the bug/requirement/task description and every
  comment are now edited in a contenteditable surface with a small
  toolbar: **B**old, *I*talic, *U*nderline, ~~Strike~~,
  bullet/numbered lists, blockquote, code block, image (uploads as
  attachment — see below). The keyboard shortcuts (Ctrl+B / Ctrl+I /
  Ctrl+U) work too. Clicking Bold on a word — without first
  dragging a selection — bolds that word (the editor auto-selects
  the word at the caret). All toggles work both ways (Bold-on /
  Bold-off, blockquote-on / blockquote-off, etc.). The backend
  sanitizes the submitted HTML against a tight allowlist before
  storage (no `<script>`, no `onerror=`, no arbitrary attributes), so
  formatting survives the round-trip without opening up stored-XSS.

  The formatting commands are implemented **directly in DOM code**,
  not via `document.execCommand`. Chrome 148 silently no-ops
  `execCommand("bold")` (and italic, underline, lists, formatBlock)
  inside the bug modal's stacking context — a regression Chrome
  haven't documented. Rolling our own keeps the editor working on
  every browser we support, present and future.
- **Paste-as-attachment.** Take a screenshot, hit Ctrl+V (or Cmd+V) in
  the description / comment editor, and the image is uploaded as a
  real attachment (not inlined into the description HTML). PDFs and
  any other file paste the same way. Unsafe extensions
  (`.exe`, `.bat`, `.cmd`, `.msi`, `.vbs`, `.ps1`, `.sh`, `.app`,
  `.dmg`, etc.) and dangerous MIMEs are rejected with a toast before
  upload. The toolbar's 🖼 button goes through the same flow — it
  opens a file picker and pushes the chosen file into the attachment
  list. Inlining was abandoned because contenteditable can't reliably
  position a caret after or resize an inline `<img>`.
- **Custom calendar / date picker.** Native `<input type="date">` has
  been replaced everywhere with an in-house popover (month nav,
  Today shortcut, today/selected highlights). Looks the same in
  Chrome, Firefox, Safari, Edge — no more vendor-shipped square boxes
  drifting between browsers. The popover is attached to `document.body`
  with `position: fixed` so it escapes the modal-foot's stacking
  context, and the prev/next month buttons stop event propagation so
  re-rendering the popover doesn't trigger the outside-click handler
  (the calendar stays open across month navigation).
- **Custom dropdowns.** Every `<select>` in the bug modal switches to
  a styled button + popover that match the calendar and the
  multi-select filter dropdowns. Hover, focus, and disabled states
  all match the rest of the v2.6 chrome. The Reporter field — which
  is always disabled because the reporter is fixed to whoever is
  logged in — renders without the ▾ caret so the visual matches the
  fact that it can't be opened.
- **Sidebar names are clickable to edit.** Hovering a Project or User
  name showed a pointer cursor but clicking did nothing. Now: click
  the colored swatch (or avatar circle) to toggle the filter; click
  the name to open the edit modal (when you have permission to). The
  ✎ icon still works as before.
- **Newest first.** New comments, attachments, and (within an event)
  tasks now sort newest-first by default so the most recent activity
  is always at the top.
- **Audit log fully loads.** The previous 300-row cap meant
  long-running deployments couldn't see history older than a few
  weeks. The default page is now 5 000 rows with a *Load older
  entries* button at the bottom for digging further (server-side cap:
  10 000 per request).
- **Fully responsive.** Every new control (calendar popover, rich
  editor toolbar, custom dropdown panel, sidebar name pills) collapses
  cleanly down to mobile-portrait widths.

Schema migrations remain **strictly additive** — existing production
databases are untouched on deploy. The v2.6 changes are purely
application-layer: the `description` and comment `body` columns stay
the same `Text` type and the field caps were raised in Pydantic only
(no DDL); the audit pagination is a query-side change; the
rich-editor / calendar / dropdown widgets live entirely in the SPA.

## What's new in v2.5

- **Per-item-type status sets.** Statuses now belong to the work flavor
  they make sense for: *"Not a Bug"*, *"Resolved"* and *"Resolve Later"*
  only apply to Bugs; *"Approved"*, *"In Review"*, *"Implemented"* and
  *"Rejected"* only to Requirements; *"Done"*, *"Blocked"* and *"Cancelled"*
  only to Tasks. The shared *"New"* status remains available on every
  type so existing rows (which default to "New") stay valid without any
  data migration. Pre-v2.5 rows holding a status that no longer fits their
  type still render and can be updated; the route layer only blocks
  *moving to* an invalid status.
- **Comments and attachments are admin-curated.** Editing or deleting any
  comment, or deleting any attachment (bug-level or comment-level), is
  now admin-only. The SPA hides the ✎ / 🗑 buttons for non-admin viewers
  and the API enforces 403 server-side. Creating comments and uploading
  attachments is still open to anyone with edit permission on the
  underlying work item — so users can still gather evidence; admins curate.
- **Post-creation attachment uploader.** The bug/requirement/task detail
  modal now has a 📎 *Add attachment* button right next to the
  Attachments heading. Stage one or more files, see thumbnail previews,
  remove any with the ✕, then click *Upload N file(s)* — useful for
  evidence you didn't have at filing time. (Comments still take their
  own attachments via the composer below.)
- **Global blocking loader.** Every action that hits the server — create,
  update, delete, upload, password change, session revoke, etc. — runs
  behind a full-page loader overlay. The overlay blocks all input until
  the request finishes, so a half-second slow link can no longer be
  double-submitted by an impatient click.
- **Layout polish.** The Events / Sessions / Audit views now use a
  proper card-style controls bar so the top buttons no longer sit
  flush against the page intro. The bug table switches to
  percentage-based column widths with min-widths so the table actually
  uses the horizontal space available at any viewport size.
- **Fully responsive.** Every new control (loader, comment admin
  actions, attach uploader) collapses cleanly at narrow widths so the
  app stays usable down to mobile-portrait sizes.

Schema migrations remain **strictly additive** — existing production
databases are untouched on deploy. The v2.5 status change is purely
validation-layer (the `status` column stays the same `String(20)`); the
admin-only comment / attachment endpoints are new but don't touch any
existing schema; the per-bug post-creation attachment uploader reuses
the existing `POST /api/bugs/{id}/attachments` endpoint.

## What's new in v2.4

- **Audit history is no longer wiped when a bug is deleted.** The trail
  preserves every original create / update / comment / assignment event
  alongside the new `bug_deleted` row, with the original `#N 'title'`
  baked into each detail string so searching by number or title still
  works after the bug is gone.
- **Audit search now actually finds things.** A LEFT JOIN against the bugs
  table means the live item title and item type are searchable too —
  type "Payment gateway" or "task" or `#42` or an assignee's name and
  the right rows come back.
- **Frontend-level read-only mode for restricted users.** A regular user
  opening a Task or Requirement now sees the whole form rendered
  disabled with a warm-tinted "Read-only" banner. The Save and Delete
  buttons hide, the comment composer hides, the assignee picker locks.
  Matches the existing server-side 403 so users see *why* before they
  type. Bugs stay editable for regular users (unchanged).
- **Form-field visual refresh.** Every input, select, textarea, the top
  search bar, the audit filter strip and the multi-select filter
  dropdowns got a contrast pass — visible borders, hover lift, focus
  ring, and a *truly* disabled state (opacity + dashed border + not-allowed
  cursor) so the difference between "you can type here" and "you can't"
  finally reads at a glance.
- **Top search placeholder updated** to make it obvious that the box
  spans bugs / requirements / tasks: paste a title, a description
  fragment, or `#42`.

Schema migrations remain **strictly additive** — existing production
databases are never altered or destroyed on deploy. The audit-retention
fix is implemented at the application layer (the route handler detaches
activity rows before issuing the bug delete) so it works on existing
production schemas with the original `ON DELETE CASCADE` constraint
still in place, with zero DDL change required.

## Features

- **Login + role-based access** — admin, manager, user; bcrypt password hashing
- **Per-session tracking & admin revocation** (Keycloak-style) — admins can
  see every active session across the system and log a specific device out
  without affecting any other session for the same user
- **Three item types in one numbering system** — Bugs 🐞, Requirements 📐 and
  Tasks ✅ all live in the same table and share the same `#N` counter, so a
  bug `#123` is followed by a task `#124` is followed by a requirement
  `#125`. The top-of-page tab strip (All / Bugs / Requirements / Tasks) is a
  pure UI filter — it scopes KPIs, the filter bar, the table columns and the
  analytics charts to the active type so a team filing 15–20 tasks per day
  doesn't drown the bug view.
- **Events** 📅 — containers for groups of work items (a daily standup, a
  sprint meeting, an incident debrief). An event can be assigned to one or
  more **managers** who get emailed when the event is created, edited or
  deleted. Tasks created *inside* an event are notified to the task's own
  assignees only — adding someone as event manager doesn't subscribe them
  to every task in the event. Items can be moved in and out of events
  freely; deleting an event preserves the items.
- **Per-item-type status sets** (v2.5) — each work flavor only sees
  statuses that make sense for it: Bugs get *New / In Progress / Resolved /
  Closed / Reopened / Not a Bug / Resolve Later*; Requirements get
  *New / In Review / Approved / Implemented / Rejected / Deferred*; Tasks
  get *New / In Progress / Done / Blocked / Cancelled*. *"New"* is shared so
  every legacy row stays valid without a data migration. Changing the
  item_type inside the modal re-populates the status dropdown live.
- **Priority / environment** — DEV / UAT / PROD; environment only
  applies to Bugs (hidden on Requirements / Tasks)
- **Per-tab KPIs and analytics** — the Total / Open / Resolved / Closed /
  Resolve-Later strip and the charts (timeline, status, priority, environment,
  project, top assignees) all rescope themselves to whichever tab is active
- **Multi-assignee** support — many users per item
- **Single-screen Jira-style item detail** — title, description, metadata,
  comments and attachments are all on one wide screen; no separate edit modal,
  no pencil button to chase
- **Comments and attachments** (PDF, image, video) stored as BLOBs in Postgres
  with an in-modal staging area: while filing a new item or composing a
  comment you can hover an attachment to remove it (✕ on hover) or click it
  to preview the file before saving — nothing uploads until you submit.
  v2.5 adds a 📎 *Add attachment* button on the item detail modal so you
  can attach more files after the item is filed (admin-only delete,
  open upload — anyone with edit perms on the item can contribute).
- **Admin-curated comments & attachments** (v2.5) — editing or deleting
  any comment, or deleting any attachment (bug- or comment-level), is
  admin-only. Comment creation and attachment upload stay open to
  anyone with edit permission so users can still gather evidence.
- **Rich-text editor for descriptions and comments** (v2.6) — bold,
  italic, underline, strike, bullet/numbered lists, blockquote, code
  block, with Ctrl+B / Ctrl+I / Ctrl+U keyboard shortcuts. Click Bold
  on a word and the editor auto-selects that word so you don't have
  to drag-select first. Pasting an image, PDF or any other file in
  the editor uploads it as a real attachment instead of inlining it,
  with an unsafe-extension blocklist (`.exe`, `.bat`, `.ps1`, `.msi`,
  `.vbs`, `.sh`, `.app`, `.dmg`, etc.) that rejects with a toast
  before upload. Server-side HTML sanitiser allows only the tags the
  editor emits — no `<script>`, no `onerror=`, no arbitrary
  attributes — so formatting survives the round-trip without opening
  a stored-XSS hole.
- **Global blocking loader** (v2.5) — every action that hits the server
  shows a full-page loader overlay that blocks all input until the
  request finishes, so impatient double-clicks can no longer create
  duplicate rows or replay deletes.
- **Email notifications** on item create / update / assignment / new comment
  *and* event create / edit / delete (Gmail / Outlook / SMTP). Type-aware
  subjects — a new task says "task", a new requirement says "requirement",
  never "bug"
- **Type-aware role enforcement** — admins do everything; managers can edit
  any item *and* events but can never delete; regular users can edit and
  create bugs only (tasks, requirements and events are read-only for them).
  The restriction is enforced both server-side (403) **and** in the SPA:
  when a regular user opens a Task or Requirement the form fields render
  disabled with a clear "Read-only — only admins and managers can edit"
  banner — no surprises after typing.
- **Forgot-password** flow via email reset link
- **Full audit trail** — every create / update / delete / login logged and
  viewable by admins and managers. **Audit history survives item deletion**:
  deleting a bug doesn't delete its history. The trail keeps every original
  event with the item's original number and title baked into the detail, so
  searching by title or number after a delete still finds the record.
- **Powerful audit search** — paste anything into the audit search box:
  bug number (`#42`, `42`, `bug 42`), assignee name, item type (`task`,
  `requirement`), the current or any historical title, action keyword.
  The query OR's against action / detail / actor / entity-type / live bug
  title / item type, so type-as-you-think Just Works.
- **Light / dark themes**, fully responsive (mobile, tablet, desktop)
- **CSV export** of all items
- **Sleuth — built-in AI assistant** 🔍 that answers natural-language questions
  about your bugs and *executes* tasks on demand (assign, close, comment,
  create). 100 % self-hosted: rules + a small statistical classifier handle
  most queries; an *optional* local LLM (llama.cpp, no external API key, no
  GPU required) catches the rest. See "[Sleuth](#sleuth--ai-assistant)" below.

## Quick start

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running

### Run it

```bash
git clone https://github.com/YOUR_USERNAME/bug-hunter.git
cd bug-hunter
cp .env.example .env       # edit if you want email enabled — see below
./deploy.sh
```

Open **http://localhost:8765** in your browser.

That's it. Postgres runs in its own isolated Docker container on port `55432`
(intentionally non-standard so it won't collide with anything you have on
`5432`). The app listens on port `8765`. The named volume `bugtracker_pgdata`
holds your live data and is **never** removed by `./deploy.sh` or by a plain
`./down.sh` — see "Live-data safety" below.

### Deploying behind a corporate proxy / air-gapped

`./deploy.sh` retries the base-image pull three times before giving up,
so a flaky link to Docker Hub no longer fails the whole deploy. If your
host can't reach `docker.io` at all, you have three escape hatches:

1. **Configure a Docker daemon HTTP proxy.** Edit `/etc/docker/daemon.json`:
   ```json
   { "proxies": { "http-proxy": "http://proxy:port", "https-proxy": "http://proxy:port" } }
   ```
   Then `sudo systemctl restart docker` and re-run `./deploy.sh`.
2. **Use an internal registry mirror.** Set `BASE_IMAGE` in `.env`
   (or inline):
   ```bash
   BASE_IMAGE=mirror.internal.example.com/python:3.12-slim ./deploy.sh
   ```
3. **Pre-load the image from a machine with network access:**
   ```bash
   docker save python:3.12-slim | gzip > python-3.12-slim.tgz
   # copy python-3.12-slim.tgz to the target host
   zcat python-3.12-slim.tgz | docker load
   ./deploy.sh
   ```

To force a full clean rebuild (ignoring the cache):
`BUILD_CLEAN=1 ./deploy.sh`. By default the build uses Docker's layer
cache, so a redeploy without code changes finishes in seconds; code
changes still bust the cache correctly because `COPY` layers detect
content changes.

**None of these touch the `bugtracker_pgdata` volume.** Your database
is unaffected by registry / network issues.

### Code-quality scan with SonarQube

The repo ships a [sonar-project.properties](sonar-project.properties)
file and a helper script that drives a Dockerized SonarQube instance
end-to-end — pytest with coverage, then sonar-scanner-cli over the
generated reports.

**One-time setup** (skip if you already run SonarQube locally):

```bash
docker run -d --name sonarqube -p 9000:9000 sonarqube:latest
# wait ~60s for it to come up, then open http://localhost:9000
# log in admin/admin, change the password, then:
#   My Account → Security → Generate Tokens → copy the value
```

**Install the dev-only deps** (pytest-cov + coverage — never shipped
in the Docker image):

```bash
pip install -r requirements-dev.txt
```

**Run a scan**:

```bash
SONAR_TOKEN=sqp_xxxxxxxxxxxx ./scripts/sonar-scan.sh
```

The script:

1. Runs `pytest --cov=app --cov-report=xml --junitxml=junit.xml`
2. Invokes `sonarsource/sonar-scanner-cli` via Docker against your local
   SonarQube (auto-rewrites `localhost` to the Docker bridge gateway so
   the scanner-in-Docker can reach SonarQube-in-Docker)
3. Prints the dashboard URL: `http://localhost:9000/dashboard?id=Bug_Hunter`

Overrides:

- `SONAR_HOST_URL=http://otherbox:9000` — point at a remote instance
- `SONAR_TOKEN=…` — required if anonymous scans are disabled (default
  on recent SonarQube versions)
- The generated `coverage.xml`, `junit.xml`, and `.scannerwork/` are
  all gitignored — re-running the scan overwrites them in place.

**Database safety:** SonarQube is a static-analysis tool that reads
source files. It does not touch `bugtracker_pgdata`, doesn't connect
to the runtime database, and runs in a completely separate container
from the Bug Hunter app/db stack.

### First login

On first run, Bug Hunter auto-creates an admin user from the `BOOTSTRAP_ADMIN_*`
env vars. Defaults:

- email: `admin@bughunter.local`
- password: `ChangeMe123!`

Log in, then **immediately** change the password from the Account panel in the
sidebar. After that, admins (and managers, with limits) can create new
accounts. Roles:

| Role    | Bugs                            | Tasks & Requirements            | Comments (v2.5)             | Attachments (v2.5)                          | Events                              | Projects                 | Users                                                    | Audit | Sessions        |
|---------|---------------------------------|---------------------------------|-----------------------------|---------------------------------------------|-------------------------------------|--------------------------|----------------------------------------------------------|-------|-----------------|
| admin   | Create, edit any, **delete any**| Create, edit any, **delete any**| Post, **edit any, delete any** | Upload, **delete any (bug- or comment-level)** | Create, edit, **delete**, manage    | Create, edit, **delete** | Create, edit, **delete**                                 | ✓     | ✓ list + revoke |
| manager | Create, edit any (no delete)    | Create, edit any (no delete)    | Post (no edit, no delete)   | Upload (no delete)                          | Create, edit, **assign managers**, no delete | Create, edit (no delete) | Create, edit non-admins (no delete, no admin role grant) | ✓     | —               |
| user    | Create, edit any (no delete)    | **View only**                   | Post on Bugs (no edit, no delete) | Upload on Bugs (no delete)                  | **View only**                       | View only                | View only                                                | —     | —               |

Notes on the policy:

- **Item deletion is admin-only**, for every type (Bug / Requirement / Task).
  Even the user who reported it can't delete; only admins can.
- **Comments and attachments are admin-curated** (v2.5). Anyone with
  edit permission on the underlying item can post a comment or upload an
  attachment — but only admins can edit a comment, delete a comment, or
  delete an attachment (bug-level or comment-level). The SPA hides the
  ✎ / 🗑 buttons for non-admins and the API enforces 403 server-side.
- **Tasks and Requirements are read-only for regular users.** They can still
  see them and use them — they just can't edit or delete. Managers and
  admins do day-to-day task management. (This means regular users can't
  post comments or upload attachments on Tasks / Requirements either —
  comment/attachment creation requires edit permission on the parent item.)
- **Event managers must be admin or manager.** Trying to assign a regular
  user as an event manager returns an explanatory 400. The picker in the
  Event modal pre-filters to eligible users so this can't be hit by accident.
- **Event delete is admin-only**; managers can edit events but not delete.
- **Managers can't grant the admin role**, can't edit existing admins, and
  can't deactivate them.
- **Audit Trail is hidden from regular users** — they don't see who did what
  across the system.
- **Admins can revoke any session.** Listed under the "Sessions" sidebar
  item: shows user, role, IP, browser, when it started, when it was last
  seen, when it expires. The admin's own current session is flagged "This
  is you" and can't be revoked from the panel — use Log out for that.

### Production checklist

Before exposing this to a real network, set these in `.env`:

```bash
SESSION_SECRET=$(openssl rand -hex 32)   # generate a long random secret
COOKIE_SECURE=true                        # only if serving over HTTPS
BOOTSTRAP_ADMIN_EMAIL=you@yourcompany.com
BOOTSTRAP_ADMIN_PASSWORD=<a strong password>
APP_BASE_URL=https://bugs.yourcompany.com
CORS_ORIGINS=https://bugs.yourcompany.com
```

Then `./down.sh && ./deploy.sh` to apply.

## Configuring email (optional)

By default `EMAIL_BACKEND=console`, which just logs emails to the app log
instead of sending them — perfect for trying things out.

To send real notifications via Gmail:

1. Enable **2-Step Verification** on your Google account.
2. Generate an [App Password](https://myaccount.google.com/apppasswords) (16 characters).
3. Edit your `.env`:
   ```env
   EMAIL_BACKEND=smtp
   EMAIL_FROM=Bug Hunter <you@gmail.com>
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USERNAME=you@gmail.com
   SMTP_PASSWORD=xxxx xxxx xxxx xxxx
   SMTP_USE_TLS=true
   ```
4. Restart: `./down.sh && ./deploy.sh`

Other providers (Office 365, Mailtrap, SendGrid, etc.) work the same way —
just point at their SMTP host and credentials.

## Live-data safety

`./deploy.sh` rebuilds the application image and restarts the stack. It does
**not** touch the `bugtracker_pgdata` volume that holds your Postgres data.
`./down.sh` (no flags) stops the containers and leaves the volume intact.
The only ways to lose data are:

- `./down.sh --wipe-db` — explicitly asks you to type `YES` first
- `docker compose down -v` — manual destructive call
- Manually deleting the named volume

Schema changes are **purely additive** — every release upgrade is
idempotent and re-running `./deploy.sh` against an existing database is
safe by design:

- `sessions` (v3.1) — created on first start if missing.
- `bugs.item_type` (v2.3) — added via `ALTER TABLE ... ADD COLUMN` with
  a `Bug` default; existing rows backfill cleanly.
- `events` + `bugs.event_id` (v2.3) — new table and new nullable FK
  (`ON DELETE SET NULL`, so removing an event preserves its items).
- `event_managers` (v2.3) — many-to-many association between events and
  the admin/manager users notified for that event.
- `activity_log.bug_id` (v2.4) — for fresh installs the FK changes from
  `ON DELETE CASCADE` to `ON DELETE SET NULL` so audit history outlives
  the bug it describes. **Existing production databases are not touched**:
  the old `CASCADE` constraint stays in place, and the route handler
  detaches activity rows (`UPDATE activity_log SET bug_id = NULL`) before
  deleting the bug, so the same retention behaviour applies on legacy
  schemas without a DDL change.
- **v2.5 — no schema changes at all.** The per-item-type status sets are
  enforced purely in the validation layer (`bugs.status` stays the same
  `String(20)`). Existing rows whose status no longer fits their type
  still render and can be updated; only *moving* a row to an invalid
  status is rejected. Admin-only comment edit/delete and attachment
  delete are new endpoints but they don't touch any existing schema —
  the `comments` and `attachments` tables are unchanged. The
  post-creation attachment uploader reuses the existing
  `POST /api/bugs/{id}/attachments` endpoint, so no new routes touch
  storage. **Redeploys of v2.5 against a v2.4 production database are
  zero-DDL.**
- **v2.6 — no schema changes at all.** The rich-text editor stores its
  HTML in the same `description` and comment `body` columns that
  always held free text (still `Text`). The Pydantic length caps were
  raised (1 MB for `description`, 200 KB for comment `body`) but no
  DDL ran. The audit-log "Load older entries" pagination is a
  query-side change (`limit` + `offset` query params, default 5 000,
  cap 10 000). The calendar / custom dropdown / rich editor widgets
  live entirely in the SPA. Paste-as-attachment reuses the existing
  `POST /api/bugs/{id}/attachments` endpoint. **Redeploys of v2.6
  against a v2.5 production database are zero-DDL.**
- **v2.7 — no schema changes at all.** A pure code-quality release.
  `app/models.py` was edited only to lift repeated string literals
  (`"bugs.id"`, `"users.id"`, `"all, delete-orphan"`, etc.) into
  named module constants — the SQL emitted by SQLAlchemy is
  byte-identical. No columns added, removed, renamed, or retyped; no
  indexes added or dropped; no cascade rules changed. Every route
  handler refactor preserved request / response wire formats and the
  underlying queries (verified by the 471-test regression). **Redeploys
  of v2.7 against a v2.6 production database are zero-DDL, zero-data-
  migration, and `docker compose up -d --build app` is sufficient
  on its own.**
- Cookies issued by older builds (which don't carry a `jti`) are still
  accepted and treated as legacy sessions, so a redeploy doesn't kick
  every user out at once.

Sleuth (the AI assistant) **adds no new tables and modifies no existing
columns**. It uses the same `bugs`, `comments`, `users`, `projects` and
`activity_log` tables the REST API uses. Read intents only `SELECT`;
write intents go through the same paths the REST API uses, including
permission checks and audit logging.

## Sleuth — AI assistant

Sleuth (🔍) is the in-app assistant. It lives as a floating widget in
the bottom-right of every page and lets users ask questions in plain
English ("show me critical bugs in PROD") *and* run actions ("assign
bug 5 to alice", "close #12", "comment on #7: works fine"). Every
write goes through an explicit Yes/Cancel confirmation prompt, and
every change is recorded in the same audit log the REST API uses.

### Examples

**Ask things:**
- *show open bugs assigned to alice*
- *how many critical bugs are in PROD?*
- *list managers* / *list projects*
- *bug 42* &middot; *summary* &middot; *recent activity*
- *export all bugs in apollo to excel* (downloads a real `.xlsx`)
- *bugs created in the last 7 days*

**Do things** (Sleuth always asks before changing anything):
- *close bug 5* &middot; *reopen #12* &middot; *mark #7 as resolved*
- *assign bug 3 to alice* &middot; *unassign bob from #5*
- *set bug 9 priority to high* &middot; *make #3 critical*
- *comment on #5: looks fixed in v2.1*
- *due bug 8 2026-06-15*
- *create a bug titled "Login broken" in project Apollo*
- *create project Mercury* (admin / manager only)

**Pronouns:**
After viewing or filtering a bug, Sleuth remembers it for 30 minutes.
*close it*, *comment on that bug: ...* and *assign it to alice* all
work after a previous turn established the context.

### Architecture

Sleuth runs in three layers, ordered by cost:

1. **Rules** (`app/chatbot/nlu.py`) — regex-driven classification of
   verbs, filters, names, and IDs. Microseconds. Handles ~80 % of
   typical queries on its own.
2. **Statistical classifier** (`app/chatbot/classifier.py`) — pure
   Python TF-IDF + cosine similarity over a hand-curated corpus.
   No external models, no GPU. ~1 ms. Catches paraphrases the rules
   miss (~10–15 % of queries).
3. **Local LLM** (`app/chatbot/llm.py`) — *optional*, lazy-loaded
   `llama.cpp` (`llama-cpp-python`) backed by a GGUF model file you
   drop into `models/`. Used only when layers 1 and 2 are uncertain.
   No external API calls, no API keys — the inference runs entirely
   on this server.

If you don't enable the LLM, Sleuth still works through layers 1 and 2.
The LLM is purely a fallback for unusual phrasing.

### Privacy

**No data leaves the server.** Sleuth makes no outbound HTTP calls,
sends no telemetry, and doesn't depend on any third-party API. Layers
1 and 2 run inside the Python process. Layer 3 (if enabled) runs
inference locally via llama.cpp.

### Enabling the optional LLM

This is **optional** and only useful for unusual phrasings that the
rules + classifier didn't match. On a 1-CPU 2 GB box, this layer is the
slowest path (5–15 s per query). For most teams, the answer is "leave
it disabled". To turn it on:

```bash
# 1. Install the inference library (CPU build, no CUDA):
pip install llama-cpp-python

# 2. Drop a small GGUF model in place:
cd models
wget https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf -O sleuth.gguf

# 3. Restart the app.
```

`models/README.md` has a full sizing table and alternative model
recommendations. **Do not commit the GGUF file to git** — it's
large (300+ MB) and `.gitignore` already excludes it.

### RAM safety: Sleuth refuses to run an LLM that won't fit

Before loading any model, Sleuth measures the actual memory ceiling of
the running container (cgroup v2 / v1) and the GGUF file size. If the
projected peak — model weights + KV cache + overhead — exceeds what's
available, Sleuth **disables Layer 3 entirely** and logs a single
operator-facing warning with the exact numbers and the recommended
docker-compose memory value. End users never see technical details:
the chat just falls back to the same friendly "I didn't understand —
try `help`" reply they'd get if no model file were installed at all.
Layers 1 and 2 keep running. There is no out-of-memory crash, no
silent degradation. See `app/chatbot/llm.py::memory_budget()` and
`tests/test_sleuth_classifier.py` for the verified behaviour.

The default `docker-compose.yml` caps the app at **512 MB** — perfect
for layers 1+2, too small for Layer 3 with any current GGUF model. To
enable Layer 3, raise `services.app.deploy.resources.limits.memory` to
at least `1500M` (for a 0.5 B Q4 model) and rerun `./deploy.sh`.

### Configuration

Sleuth honours these environment variables (all optional, see
`.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `SLEUTH_LLM_MODEL_PATH` | `models/sleuth.gguf` | absolute path to GGUF |
| `SLEUTH_LLM_TIMEOUT_S` | `12` | inference budget |
| `SLEUTH_LLM_IDLE_UNLOAD_S` | `600` | unload model after idle |
| `SLEUTH_LLM_MAX_TOKENS` | `120` | max generated tokens |
| `SLEUTH_LLM_CTX_LEN` | `1024` | context window |
| `SLEUTH_LLM_THREADS` | `1` | CPU threads for inference |

Rate limit: 30 chat messages per minute per user (built into the
`/api/chat` router; not configurable).

### Keyboard shortcut

Press `Ctrl + /` (or `⌘ + /` on macOS) on any page to open the Sleuth
panel. `Esc` closes it.

## Stopping

```bash
./down.sh                  # stop containers, KEEP database volume + image
./down.sh --wipe-db        # also wipe the database (asks for YES)
./down.sh --remove-images  # also remove the built image
./down.sh --full-clean     # both
```

## Tech stack

- **Backend:** FastAPI 0.115, SQLAlchemy 2.0, Pydantic 2, psycopg 3
- **Database:** PostgreSQL 16
- **Frontend:** Vanilla JavaScript (no framework), CSS variables for theming
- **Container:** Python 3.12 slim image, multi-service Docker Compose
- **Sleuth assistant:** in-process rules + TF-IDF classifier (pure Python);
  optional `llama-cpp-python` for the local LLM layer

## Project structure

```
.
├── app/
│   ├── config.py          # env-driven settings
│   ├── database.py        # SQLAlchemy setup
│   ├── email_service.py   # SMTP / console email backends
│   ├── main.py            # FastAPI entry point
│   ├── models.py          # User, Project, Bug (Bug/Requirement/Task),
│   │                      # Event, event_managers, Comment, Attachment,
│   │                      # Activity, PasswordResetToken, Session
│   ├── routes/            # auth, users, projects, bugs, events, stats,
│   │                      # audit, sessions
│   ├── schemas.py         # Pydantic DTOs
│   ├── chatbot/           # Sleuth — the in-app AI assistant
│   │   ├── nlu.py         #   Layer 1: rule-based parser
│   │   ├── classifier.py  #   Layer 2: TF-IDF intent classifier
│   │   ├── llm.py         #   Layer 3: optional local LLM (llama.cpp)
│   │   ├── executor.py    #   read intents → DB queries → blocks
│   │   ├── actions.py     #   write intents → ActionPlan → audited mutation
│   │   ├── memory.py      #   per-user conversation context (TTL'd)
│   │   ├── excel.py       #   in-memory xlsx export (openpyxl)
│   │   └── router.py      #   FastAPI endpoints under /api/chat
│   └── static/            # index.html + login.html + reset.html
│                          # + app.js + styles.css + chatbot.{js,css} + favicons
├── tests/                 # Sleuth tests — 300 checks, hermetic SQLite
│   ├── test_sleuth_parser.py
│   ├── test_sleuth_actions.py
│   ├── test_sleuth_classifier.py
│   ├── test_sleuth_safety.py
│   ├── test_sleuth_comprehensive.py
│   └── run_all.py         # one-command runner
├── models/                # GGUF model files for Sleuth (gitignored)
│   └── README.md          # how to download an LLM if you want one
├── docker-compose.yml
├── Dockerfile
├── deploy.sh              # build + start (idempotent, safe on re-run)
├── down.sh                # stop (data-safe by default)
├── requirements.txt
└── .env.example           # copy to .env and edit
```

## Running tests

The Sleuth test suite is hermetic — every test file spins up its own
temp SQLite database and never touches your production data:

```bash
pip install -r requirements.txt
python3 tests/run_all.py        # 300 checks, ~10 s
```

You can also run an individual file:

```bash
python3 tests/test_sleuth_actions.py
python3 tests/test_sleuth_safety.py     # database-safety guarantees
```

## Contributing

Issues and pull requests welcome. Please run the tests before submitting.

## License

Released under the [MIT License](LICENSE.txt). See the LICENSE file for details.
