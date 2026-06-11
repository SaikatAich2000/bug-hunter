# 🐞 Bug Hunter

A self-hosted, internal-use issue tracker. FastAPI + PostgreSQL + a
zero-framework JavaScript SPA. One Docker command to run, no external
auth, no external file storage — attachments live in the database.

**Current version: v2.10** — adds a comprehensive **Reports** view (Jira-
style report builder with 9 report types, universal filters, full
Excel export including raw item data) plus a "report" intent in the
**Sleuth** chatbot so the same reports can be requested in natural
language. The legacy single-table CSV export was retired in favour of
the multi-sheet XLSX export.
**Zero schema changes** since v2.4; production databases are byte-for-byte
untouched on every upgrade. See *[Live-data safety](#live-data-safety)*.

---

## Features

- **Three item types in one numbering system** — Bugs 🐞, Requirements 📐
  and Tasks ✅ share one `#N` counter. The tab strip (All / Bugs /
  Requirements / Tasks) scopes the KPIs, filters, table columns, and
  analytics to the active type.
- **Per-item-type status sets** — Bugs get *New / In Progress / Resolved
  / Closed / Reopened / Not a Bug / Resolve Later*; Requirements get
  *New / In Review / Approved / Implemented / Rejected / Deferred*;
  Tasks get *New / In Progress / Done / Blocked / Cancelled*.
- **Events** 📅 — containers for groups of work items with one or more
  managers (admin/manager only). Tasks created inside an event notify
  the task's own assignees, not the event managers.
- **Priority + environment** — DEV / UAT / PROD; environment only
  applies to Bugs.
- **Per-tab KPIs and analytics** — timeline, status, priority,
  environment, project, top-assignees charts all rescope to the
  active tab.
- **Rich-text editor** for descriptions and comments (bold, italic,
  underline, strike, lists, blockquote, code block, Ctrl+B/I/U). Paste
  an image / PDF and it uploads as a real attachment (unsafe extensions
  rejected before upload). Server-side HTML sanitiser blocks `<script>`,
  `onerror=`, and unknown attributes.
- **Comments + attachments** (PDF, image, video) stored as Postgres
  BLOBs. Anyone with edit permission can post; only admins can edit
  or delete comments and only admins can delete attachments.
- **Login + role-based access** (admin / manager / user, bcrypt). Type-
  aware enforcement: regular users can edit Bugs only — Tasks and
  Requirements render disabled with a "Read-only" banner.
- **Per-session tracking + admin revocation** — admins see every active
  session (user, role, IP, browser, started, last seen, expires) and
  can log a specific device out without affecting other sessions.
- **Forgot-password flow** via email reset link.
- **Full audit trail** — every create / update / delete / login logged
  for admins and managers; history survives item deletion (original
  number + title baked into the detail).
- **Audit search** OR-matches action / detail / actor / entity-type /
  live bug title / item type, so `#42`, an assignee name, a typeword,
  or any historical title all return the right rows.
- **Email notifications** (Gmail / Outlook / SMTP) on item / event
  create / update / delete / assignment / new comment. Type-aware
  subjects.
- **Global blocking loader** — every server-side action sits behind a
  full-page overlay until it finishes, so double-clicks can't replay.
- **Custom calendar + dropdowns** — consistent appearance across
  Chrome, Firefox, Safari, Edge. No vendor-shipped square boxes.
- **Sleuth — in-app AI assistant** 🔍. Natural-language questions
  ("open bugs in PROD") and audited actions ("close #5", "assign #3
  to alice"). 100% self-hosted. See *[Sleuth](#sleuth--ai-assistant)*.
- **Light / dark themes**, fully responsive (mobile → desktop), CSV
  export.

---

## Quick start

**Prerequisites:** [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
git clone https://github.com/YOUR_USERNAME/bug-hunter.git
cd bug-hunter
cp .env.example .env       # edit if you want email enabled — see below
./deploy.sh
```

Open **<http://localhost:8765>**. Postgres runs in its own container on
port `55432` (deliberately non-standard). The named volume
`bugtracker_pgdata` holds your data and is **never** removed by
`./deploy.sh` or `./down.sh` — see *[Live-data safety](#live-data-safety)*.

For a forced clean rebuild: `BUILD_CLEAN=1 ./deploy.sh`. Behind a
corporate proxy or air-gapped, set `BASE_IMAGE` in `.env` to an
internal mirror, or pre-load `python:3.12-slim` with
`docker save | docker load`.

### First login

The bootstrap admin is created from the `BOOTSTRAP_ADMIN_*` env vars.
Defaults: `admin@bughunter.local` / `ChangeMe123!`. **Change the
password immediately** from the Account panel.

### Roles in one sentence

**Admins** do everything. **Managers** edit any item or event but can
never delete, can't grant the admin role, and can't edit existing admins.
**Regular users** can create + edit Bugs only; Tasks, Requirements, and
Events are read-only for them. Item deletion is admin-only across every
type. Comment edit / delete and attachment delete are admin-only.

### Production checklist

```bash
SESSION_SECRET=$(openssl rand -hex 32)
COOKIE_SECURE=true                        # only if serving over HTTPS
BOOTSTRAP_ADMIN_EMAIL=you@yourcompany.com
BOOTSTRAP_ADMIN_PASSWORD=<a strong password>
APP_BASE_URL=https://bugs.yourcompany.com
CORS_ORIGINS=https://bugs.yourcompany.com
```

Then `./down.sh && ./deploy.sh`.

---

## Configuring email (optional)

By default `EMAIL_BACKEND=console` logs emails to stdout. To send real
mail via Gmail: enable 2-Step Verification, generate an [App
Password](https://myaccount.google.com/apppasswords), then in `.env`:

```env
EMAIL_BACKEND=smtp
EMAIL_FROM=Bug Hunter <you@gmail.com>
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=xxxx xxxx xxxx xxxx
SMTP_USE_TLS=true
```

Restart with `./down.sh && ./deploy.sh`. Other providers (Office 365,
Mailtrap, SendGrid) work the same way — point at their SMTP host.

---

## Live-data safety

`./deploy.sh` rebuilds the image and restarts the stack. It does **not**
touch the `bugtracker_pgdata` volume that holds your Postgres data.
`./down.sh` (no flags) stops containers and leaves the volume intact.
The only ways to lose data are explicitly opt-in:

- `./down.sh --wipe-db` (asks you to type `YES`)
- `docker compose down -v` (manual destructive call)
- Manually deleting the named volume

**Schema migrations are strictly additive.** Every release upgrade is
idempotent against an existing database:

- `sessions` (v3.1) — created on first start if missing.
- `bugs.item_type` (v2.3) — `ALTER TABLE ... ADD COLUMN` with a `Bug`
  default; existing rows backfill cleanly.
- `events` + `bugs.event_id` (v2.3) — new table, new nullable FK with
  `ON DELETE SET NULL`.
- `event_managers` (v2.3) — new many-to-many table.
- `activity_log.bug_id` (v2.4) — fresh installs use `ON DELETE SET
  NULL` so audit history outlives the bug. Existing production
  databases keep the old `CASCADE` constraint; the route handler
  detaches activity rows before deleting the bug, so the same
  retention applies on legacy schemas without a DDL change.
- **v2.5, v2.6, v2.7 — no schema changes at all.** Status sets, the
  rich-text editor, the audit pagination, and the v2.7 code-quality
  pass are all application-layer. Redeploys against a prior
  production database are zero-DDL.

Cookies issued by older builds (without a `jti`) are still accepted as
legacy sessions, so a redeploy doesn't kick every user out at once.

Sleuth adds **no tables and modifies no columns**. Read intents only
`SELECT`; write intents go through the same audited paths the REST
API uses.

---

## Release notes

Full history lives in [CHANGELOG.md](CHANGELOG.md). Latest: **v2.10** —
Reports view + Sleuth report intent, multi-sheet XLSX export, legacy
CSV export retired. No DB schema change.

## Contributing & security

- Bug reports / feature ideas — GitHub Issues.
- Code contributions — see [CONTRIBUTING.md](CONTRIBUTING.md).
- Vulnerabilities — **don't open a public issue**; see
  [SECURITY.md](SECURITY.md) for the private disclosure path.

---

## Sleuth — AI assistant

Sleuth (🔍) is the in-app assistant — a floating widget in the
bottom-right of every page. Press `Ctrl + /` (or `⌘ + /`) to open.

**Ask things:**

- *show open bugs assigned to alice*
- *how many critical bugs are in PROD?*
- *bug 42* · *summary* · *recent activity*
- *bugs created in the last 7 days*
- *export all bugs in apollo to excel* (returns a real `.xlsx`)

**Do things** (always confirmed before changing anything, always audited):

- *close bug 5* · *reopen #12* · *mark #7 as resolved*
- *assign bug 3 to alice* · *unassign bob from #5*
- *set bug 9 priority to high* · *due bug 8 2026-06-15*
- *comment on #5: looks fixed in v2.1*
- *create a bug titled "Login broken" in project Apollo*

**Pronouns:** after a turn that named a bug, *close it* /
*comment on that bug: …* / *assign it to alice* work for 30 minutes.

### Architecture

Three layers, cheapest first:

1. **Rules** (`app/chatbot/nlu.py`) — regex classifier of verbs,
   filters, names, IDs. Microseconds. Handles ~80% of queries.
2. **Statistical classifier** (`app/chatbot/classifier.py`) — TF-IDF
   + cosine similarity over a hand-curated corpus, no external models.
   ~1 ms. Catches paraphrases (~10–15%).
3. **Local LLM** (`app/chatbot/llm.py`) — *optional*, lazy-loaded
   `llama.cpp` against a GGUF model in `models/`. Only used when
   layers 1 + 2 are uncertain.

**No data leaves the server.** No outbound HTTP, no telemetry, no
third-party API. Even Layer 3 runs inference locally.

### Optional LLM

Useful only for unusual phrasings the rules / classifier missed. On a
1-CPU 2 GB box this is the slowest path (5–15 s per query); leave
disabled for most teams. To enable:

```bash
pip install llama-cpp-python
cd models && wget https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf -O sleuth.gguf
# raise services.app.deploy.resources.limits.memory to 1500M in docker-compose.yml
./deploy.sh
```

**RAM safety:** before loading the model, Sleuth measures the
container's actual memory ceiling (cgroup v2/v1) and the projected
peak (weights + KV cache + overhead). If it won't fit, Layer 3 is
disabled entirely with an operator-facing warning. Users see the same
friendly "I didn't understand" fallback they'd get if no model file
existed. No OOM crashes. See `app/chatbot/llm.py::memory_budget()`.

See `models/README.md` for a sizing table and alternative models. The
GGUF file is gitignored — do not commit it.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SLEUTH_LLM_MODEL_PATH` | `models/sleuth.gguf` | absolute path to GGUF |
| `SLEUTH_LLM_TIMEOUT_S` | `12` | inference budget |
| `SLEUTH_LLM_IDLE_UNLOAD_S` | `600` | unload after idle |
| `SLEUTH_LLM_MAX_TOKENS` | `120` | max generated tokens |
| `SLEUTH_LLM_CTX_LEN` | `1024` | context window |
| `SLEUTH_LLM_THREADS` | `1` | CPU threads |

Rate limit: 30 chat messages per minute per user (built into
`/api/chat`; not configurable).

---

## Stopping

```bash
./down.sh                  # stop containers, KEEP database volume + image
./down.sh --wipe-db        # also wipe the database (asks for YES)
./down.sh --remove-images  # also remove the built image
./down.sh --full-clean     # both
```

---

## Code-quality scan (SonarQube)

The repo ships `sonar-project.properties` and `scripts/sonar-scan.{sh,ps1}`
that drive a Dockerized SonarQube end-to-end (pytest with coverage, then
sonar-scanner-cli over the generated reports).

```bash
docker run -d --name sonarqube -p 9000:9000 sonarqube:latest
# wait ~60s, log in admin/admin, change password,
# My Account → Security → Generate Tokens → copy the value
pip install -r requirements-dev.txt
SONAR_TOKEN=sqp_xxxxxxxxxxxx ./scripts/sonar-scan.sh
```

Dashboard: `http://localhost:9000/dashboard?id=Bug_Hunter`. Override
`SONAR_HOST_URL` for a remote instance. The generated `coverage.xml`,
`junit.xml`, and `.scannerwork/` are gitignored.

SonarQube is purely static analysis — it does not touch the runtime
database.

---

## Running tests

The test suite is hermetic — every test file spins up its own temp
SQLite database and never touches your production data.

```bash
pip install -r requirements-dev.txt
pytest                                 # 471 checks, ~6 min with coverage
pytest tests/test_sleuth_actions.py    # one file
pytest tests/test_sleuth_safety.py     # database-safety guarantees
```

---

## Tech stack

FastAPI 0.115 · SQLAlchemy 2.0 · Pydantic 2 · psycopg 3 · PostgreSQL 16
· vanilla JS SPA · Python 3.12 slim container. Sleuth: in-process rules
+ TF-IDF classifier (pure Python); optional `llama-cpp-python` for the
local LLM layer.

---

## Project structure

```
app/
├── config.py · database.py · email_service.py · main.py · schemas.py
├── models.py             # User, Project, Bug (Bug/Requirement/Task),
│                         # Event, event_managers, Comment, Attachment,
│                         # Activity, PasswordResetToken, Session
├── routes/               # auth · users · projects · bugs · events
│                         # · stats · audit · sessions
├── chatbot/              # Sleuth (rules · classifier · LLM · executor
│                         # · actions · memory · excel · router)
└── static/               # index.html · login.html · reset.html
                          # · app.js · styles.css · chatbot.{js,css}
tests/                    # 471 hermetic SQLite-backed tests
models/                   # GGUF files for Sleuth (gitignored)
scripts/sonar-scan.*      # SonarQube scan driver
deploy.sh · down.sh       # idempotent + data-safe
docker-compose.yml · Dockerfile · requirements.txt · .env.example
```

---

## Contributing

Issues and pull requests welcome. Please run the tests before
submitting.

## License

[MIT](LICENSE.txt).
