# 🐞 Bug Hunter

A self-hosted issue tracker built on **FastAPI + PostgreSQL** with a
**React + TypeScript** single-page app. It runs as a single Docker
command, needs no external auth provider and no external file storage —
attachments live in the database. Bugs, requirements and tasks share one
numbering scheme; projects, events, comments, a full audit trail, email
and web-push notifications, reports, and an optional in-app AI assistant
("Sleuth") round it out.

**Current version: v3.0.** The frontend is a React 18 + TypeScript SPA,
there's a per-user in-app notification system (top-bar bell + unread
badge + `/api/notifications`), and the UI got a full UX overhaul (light +
dark themes). Schema changes are strictly additive, so existing
production databases are untouched on upgrade. See
[CHANGELOG.md](CHANGELOG.md) and *[Live-data safety](#live-data-safety)*.

A companion native **Android app** lives in a separate repository and
registers into the same FCM push table.

---

## Features

- **Three item types, one numbering system** — Bugs 🐞, Requirements 📐
  and Tasks ✅ share one `#N` counter. The tab strip (All / Bugs /
  Requirements / Tasks) scopes KPIs, filters, table columns, and
  analytics to the active type.
- **Per-item-type status sets** — Bugs: *New / In Progress / Resolved /
  Closed / Reopened / Not a Bug / Resolve Later*; Requirements: *New / In
  Review / Approved / Implemented / Rejected / Deferred*; Tasks: *New / In
  Progress / Done / Blocked / Cancelled*.
- **Projects & events** 📅 — events are containers for groups of work
  items with one or more managers (admin/manager only).
- **Priority + environment** — DEV / UAT / PROD; environment applies to
  Bugs only.
- **Per-tab KPIs and analytics** — timeline, status, priority,
  environment, project and top-assignee charts all rescope to the active
  tab.
- **Rich-text editor** for descriptions and comments (bold, italic,
  underline, strike, lists, blockquote, code). Paste an image / PDF and
  it uploads as a real attachment; unsafe extensions are rejected and a
  server-side HTML sanitiser blocks `<script>`, `onerror=`, etc.
- **Comments + attachments** (PDF, image, video) stored as Postgres
  BLOBs. Edit permission lets anyone comment; only admins edit/delete
  comments or delete attachments.
- **Login + role-based access** (admin / manager / user, bcrypt). Regular
  users can edit Bugs only; Tasks and Requirements render read-only.
- **Per-session tracking + admin revocation** — admins see every active
  session (user, role, IP, browser, started, last seen, expires) and can
  log out a single device.
- **Forgot-password flow** via emailed reset link.
- **Full audit trail** — every create / update / delete / login is logged
  for admins and managers and survives item deletion.
- **Notifications** — per-user in-app bell, email (per-operation or a
  daily digest), and optional browser/FCM web push.
- **Reports** — Jira-style report builder (manager/admin) with multi-sheet
  Excel export.
- **Sleuth — in-app AI assistant** 🔍. Natural-language questions
  ("open bugs in PROD") and audited actions ("close #5", "assign #3 to
  alice"). Runs locally by default. See *[Sleuth](#sleuth--ai-assistant)*.
- **Light / dark themes**, responsive (mobile → desktop), CSV/Excel export.

---

## Quick start

**Prerequisites:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(Docker Engine + Compose v2).

```bash
git clone https://github.com/<your-org>/bug-hunter.git
cd bug-hunter
cp .env.example .env       # edit the values you care about — see Configuration
./deploy.sh
```

`./deploy.sh` builds the image (`docker compose --env-file .env build`),
starts Postgres, waits for it to be healthy, then starts the app. Open
**<http://localhost:8765>**.

Postgres runs in its own container, published on host port `55432`
(deliberately non-standard to avoid clashing with a local Postgres). Your
data lives in the named volume `bugtracker_pgdata` and is **never**
removed by `./deploy.sh` or `./down.sh` — see
*[Live-data safety](#live-data-safety)*.

For a forced clean rebuild: `BUILD_CLEAN=1 ./deploy.sh`. Behind a
corporate proxy or air-gapped, set `BASE_IMAGE` in `.env` to an internal
mirror, or pre-load `python:3.12-slim` with `docker save | docker load`.

### First login

On first run (an empty database) a bootstrap admin is created from the
`BOOTSTRAP_ADMIN_*` env vars. Defaults: `admin@bughunter.local` /
`ChangeMe123!`. **Change the password immediately** from the top-right
profile menu → *Change password*.

### Roles in one sentence

**Admins** do everything. **Managers** edit any item or event but can
never delete, grant the admin role, or edit existing admins. **Regular
users** can create and edit Bugs only. Deletion is admin-only across every
type, as are comment edit/delete and attachment delete.

### Production checklist

```bash
SESSION_SECRET=$(openssl rand -hex 32)    # required in prod; never blank
COOKIE_SECURE=true                        # only if serving over HTTPS
BOOTSTRAP_ADMIN_EMAIL=you@example.com
BOOTSTRAP_ADMIN_PASSWORD=<a strong password>
APP_BASE_URL=https://bugs.example.com
CORS_ORIGINS=https://bugs.example.com
```

Then `./down.sh && ./deploy.sh`.

---

## Configuration

All configuration is read from environment variables; the committed
[`.env.example`](.env.example) is the template — copy it to `.env` and
edit. The full list (with inline comments) lives in `.env.example` and is
parsed in `app/config.py`. The most important variables:

| Variable | Default | Purpose |
|---|---|---|
| `SESSION_SECRET` | _(blank)_ | Signs session cookies. **Set a long random value in prod** (`openssl rand -hex 32`); blank means a new random secret per restart, logging everyone out. |
| `COOKIE_SECURE` | `false` | Set `true` only when serving over HTTPS. |
| `APP_BASE_URL` | `http://localhost:8765` | Public URL used in email links. |
| `CORS_ORIGINS` | _(blank = same-origin)_ | Comma-separated allow-list of cross-origin clients. |
| `BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` / `_NAME` | `admin@bughunter.local` / `ChangeMe123!` / `Admin` | First-run admin (only when the DB has zero users). |
| `EMAIL_BACKEND` | `console` | `console` (log to stdout), `smtp`, or `disabled`. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_USE_TLS` | — | SMTP delivery when `EMAIL_BACKEND=smtp`. |
| `EMAIL_DIGEST_ENABLED` | `false` | Batch per-operation emails into one daily digest. |
| `WEB_PUSH_ENABLED` | `false` | Master switch for browser push (FCM). |
| `FCM_CREDENTIALS_FILE` | _(blank)_ | Path to the Firebase service-account JSON (mounted secret). |
| `FIREBASE_*` | _(blank)_ | Firebase web-app config + VAPID key for push. |
| `SLEUTH_CLOUD_ENABLED` | `false` | Opt-in cloud LLM fallback for Sleuth (Gemini/OpenRouter). |
| `GEMINI_API_KEY` / `OPENROUTER_API_KEY` | _(blank)_ | Keys for the optional cloud LLM layer. |

> Secrets (`.env`, `secrets/firebase-admin.json`) are gitignored and
> provisioned per host. Never commit real secrets — reference the env-var
> names only.

### Configuring email (optional)

By default `EMAIL_BACKEND=console` logs emails to stdout. To send real
mail via Gmail, enable 2-Step Verification, generate an
[App Password](https://myaccount.google.com/apppasswords), then in `.env`:

```env
EMAIL_BACKEND=smtp
EMAIL_FROM=Bug Hunter <you@example.com>
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@example.com
SMTP_PASSWORD=xxxx xxxx xxxx xxxx
SMTP_USE_TLS=true
```

Restart with `./down.sh && ./deploy.sh`. Office 365, Mailtrap, SendGrid,
etc. work the same way — point at their SMTP host.

### Daily email digest (optional)

By default every work-item operation (new item, update, assignment,
comment, event) sends its own email immediately. Set
`EMAIL_DIGEST_ENABLED=true` to switch to **one digest email per user per
day** instead, grouped into five sections — *Assigned to you*, *Reported
by/with you*, *Updates*, *Comments*, *Events*. **Password-reset and other
security emails always send immediately — they are never batched.**

The digest is a standalone job; schedule it however you already run jobs:

```bash
# Linux host cron — 7:00am daily (crontab -e):
0 7 * * *  cd /opt/bug-hunter && docker compose exec -T app python -m app.jobs.email_digest

# …or run the module directly outside Docker:
python -m app.jobs.email_digest
```

On Windows, point Task Scheduler at the same command. The job is
**idempotent** (it stamps each operation as it sends, via the additive
`notifications.emailed_at` column, so a double run never double-sends) and
**bounded** (`EMAIL_DIGEST_LOOKBACK_HOURS`, default 26h). It's a no-op
when the flag is off, so it's safe to leave scheduled either way.

### Web push notifications (optional)

Browser push via **Firebase Cloud Messaging (FCM)**, sent **immediately**
when an operation happens (new item / update / assignment / comment /
event) — independent of the email digest. Default off. The same backend
table and send path are FCM-token-based, so the companion Android app
registers into them with no backend rework.

**One-time Firebase setup:**

1. Create (or reuse) a project at <https://console.firebase.google.com>.
2. **Add a Web app** (Project settings → General → *Your apps* → Web) and
   copy the config into `.env`: `FIREBASE_API_KEY`, `FIREBASE_AUTH_DOMAIN`,
   `FIREBASE_PROJECT_ID`, `FIREBASE_MESSAGING_SENDER_ID`, `FIREBASE_APP_ID`.
3. **Generate a Web Push key pair** (Project settings → *Cloud Messaging* →
   *Web configuration* → *Web Push certificates* → *Generate key pair*) and
   put the public key in `FIREBASE_VAPID_KEY`.
4. **Download a service-account key** (Project settings → *Service
   accounts* → *Generate new private key*). Save it as
   `secrets/firebase-admin.json` — `docker-compose.yml` mounts that path
   read-only into the container and points `FCM_CREDENTIALS_FILE` at it.
5. Set `WEB_PUSH_ENABLED=true` and restart. **Serve the app over HTTPS** —
   browsers only allow push on a secure origin (`localhost` is exempt for
   dev). On the Android app, push works over plain HTTP (no secure-origin
   rule applies).

Each user then clicks **"Enable push notifications"** once in the profile
menu to grant their browser permission. Everything else (the self-hosted
Firebase SDK under `/static/vendor`, the `/firebase-messaging-sw.js`
service worker, the `push_subscriptions` table, token registration and
pruning) is built in. Adding the feature is additive (one new table); your
production data is untouched.

---

## Local frontend development

The SPA source lives in `frontend/` (React + TypeScript + Vite). The build
emits the static bundle into `app/static/`, which FastAPI serves directly
— so after editing the frontend you rebuild, and the running app picks up
the new bundle.

```bash
cd frontend
npm install
npm run build          # type-checks, vendors the Firebase SDK, writes app/static
```

For a fast inner loop, `npm run dev` runs the Vite dev server; for the full
production-equivalent output, use `npm run build` and reload the app.

---

## Live-data safety

`./deploy.sh` rebuilds the image and restarts the stack. It does **not**
touch the `bugtracker_pgdata` volume that holds your Postgres data.
`./down.sh` (no flags) stops the containers and leaves the volume intact.
The only ways to lose data are explicitly opt-in:

- `./down.sh --wipe-db` (asks you to type `YES`)
- `docker compose down -v` (manual destructive call)
- Manually deleting the named volume

**Schema migrations are strictly additive.** Every upgrade is idempotent
against an existing database: new tables/columns are created by
`init_db()` on boot via `Base.metadata.create_all()`, and existing rows
are never modified. Cookies issued by older builds (without a `jti`) are
still accepted as legacy sessions, so a redeploy doesn't sign everyone out
at once.

Sleuth adds **no tables and modifies no columns**: read intents only
`SELECT`; write intents go through the same audited paths as the REST API.

---

## Sleuth — AI assistant

Sleuth (🔍) is the in-app assistant — a floating widget on every page.
Press `Ctrl + /` (or `⌘ + /`) to open.

**Ask things:**

- *show open bugs assigned to alice*
- *how many critical bugs are in PROD?*
- *bug 42* · *summary* · *recent activity*
- *bugs created in the last 7 days*
- *export all bugs in apollo to excel* (returns a real `.xlsx`)
- *report of who solved how many bugs last week*

**Do things** (always confirmed before changing anything, always audited):

- *close bug 5* · *reopen #12* · *mark #7 as resolved*
- *assign bug 3 to alice* · *unassign bob from #5*
- *set bug 9 priority to high* · *due bug 8 2026-06-15*
- *comment on #5: looks fixed in v2.1*
- *create a bug titled "Login broken" in project Apollo*

After a turn that named a bug, pronouns (*close it*, *assign it to
alice*) resolve for 30 minutes.

### Architecture

Sleuth tries the cheapest layer first and only escalates when needed:

1. **Rules** (`app/chatbot/nlu.py`) — regex classifier of verbs, filters,
   names and IDs. Microseconds. Handles the bulk of queries.
2. **Statistical classifier** (`app/chatbot/classifier.py`) — TF-IDF +
   cosine similarity over a hand-curated corpus, no external models.
   Catches paraphrases.
3. **Local LLM** (`app/chatbot/llm.py`) — *optional*, lazy-loaded
   `llama.cpp` against a GGUF model in `models/`. Used only when layers
   1 + 2 are uncertain. See [models/README.md](models/README.md).
4. **Cloud LLM** (`app/chatbot/cloud_llm.py`) — *optional, off by
   default*. When `SLEUTH_CLOUD_ENABLED=1` and a key is set, free-form
   questions can fall through to Gemini (primary) / OpenRouter (fallback),
   optionally with RAG retrieval (`app/chatbot/rag.py`, needs `chromadb`).

**Privacy:** with the defaults (`SLEUTH_CLOUD_ENABLED=0`, no model file),
Sleuth is **fully local** — no outbound HTTP, no telemetry, no third-party
API; even the optional Layer 3 LLM runs inference on the server. Layer 4
is the only path that sends text to an external provider, and it is **off
unless you explicitly enable it**. In every mode Sleuth **never writes
data through the model and never invents counts** — data questions are
answered by deterministic SQL handlers.

### Configuration

Local-LLM tuning lives in [models/README.md](models/README.md). Key
cloud-layer variables (all default off / blank):

| Variable | Default | Purpose |
|---|---|---|
| `SLEUTH_CLOUD_ENABLED` | `0` | Enable the cloud LLM fallback. |
| `GEMINI_API_KEY` | _(blank)_ | Google AI Studio key (primary provider). |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Cloud model id. |
| `OPENROUTER_API_KEY` | _(blank)_ | Fallback provider key. |
| `SLEUTH_RAG_ENABLED` | `0` | Document retrieval over bugs/comments/docs. |

Rate limit: 30 chat messages per minute per user (built into `/api/chat`).

---

## Running tests

The suite is hermetic — each test file spins up its own temp SQLite
database and never touches your production data.

```bash
pip install -r requirements-dev.txt
pytest                                  # full suite with coverage
pytest tests/test_push.py               # a single file
```

UI smoke tests use Playwright + Chromium:

```bash
python -m playwright install chromium
pytest tests/test_ui_smoke.py
```

SonarQube config is in `sonar-project.properties` (see
`scripts/sonar-scan.{sh,ps1}`). Static analysis only — it never touches
the runtime database.

---

## Stopping

```bash
./down.sh                  # stop containers, KEEP database volume + image
./down.sh --wipe-db        # also wipe the database (asks for YES)
./down.sh --remove-images  # also remove the built image
./down.sh --full-clean     # both
```

---

## Tech stack

FastAPI · SQLAlchemy 2.0 · Pydantic 2 · psycopg 3 · PostgreSQL 16 ·
Python 3.12-slim container. Frontend: React 18 + TypeScript built with
Vite. Sleuth: in-process rules + TF-IDF classifier (pure Python),
optional local `llama-cpp-python`, optional cloud LLM (Gemini/OpenRouter).

---

## Project structure

```
app/
├── config.py · database.py · main.py · schemas.py · models.py
├── auth.py · email_service.py · notification_service.py · push_service.py
├── routes/      # auth · users · projects · bugs · events · stats
│                # · audit · sessions · reports · notifications · push
├── chatbot/     # Sleuth: nlu · classifier · llm · cloud_llm · rag
│                # · executor · actions · memory · excel · router
├── jobs/        # email_digest (scheduled digest job)
└── static/      # built React bundle (index.html, assets, vendor SDK)
frontend/        # React + TypeScript SPA source (Vite) → builds into app/static
tests/           # hermetic SQLite-backed pytest suite
models/          # GGUF files for Sleuth's optional local LLM (gitignored)
deploy.sh · down.sh             # data-safe build/start + stop
docker-compose.yml · Dockerfile · requirements.txt · .env.example
```

---

## Contributing & security

- Bug reports / feature ideas — GitHub Issues.
- Code contributions — see [CONTRIBUTING.md](CONTRIBUTING.md). Run the
  tests before submitting.
- Vulnerabilities — **don't open a public issue**; see
  [SECURITY.md](SECURITY.md) for the private disclosure path.

## License

[MIT](LICENSE.txt).
