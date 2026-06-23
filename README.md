# Bug Hunter

Bug Hunter is a self-hosted tracker for bugs, requirements, and tasks. It uses a
FastAPI + PostgreSQL backend and a React + TypeScript frontend, and runs as one
Docker Compose stack. There is no external login provider to set up, and
attachments are stored in the database — so one database backup saves everything.

On top of work items, it gives you projects, events, comments, an audit trail,
notifications, reports, and an optional in-app assistant called **Sleuth**.

Current version: **3.0**. Upgrades only *add* tables and columns, never remove or
change existing data, so upgrading never touches your production data (see
[Live-data safety](#live-data-safety)).

A companion native Android app lives in its own repository and uses the same
web-push system.

## Features

| Area | What it does |
|---|---|
| Work items | Bugs, requirements, and tasks share one `#N` counter. A tab strip (All / Bugs / Requirements / Tasks) filters the KPIs, columns, and analytics to the type you pick. Each type has its own statuses; bugs also have a DEV/UAT/PROD environment. |
| Projects & events | Projects group your work. Events group items for a standup or sprint and have one or more managers. |
| Item links | Link items together: relates, blocks, or duplicate. |
| Comments & attachments | Rich-text descriptions and comments (bold, italic, lists, code, quotes). PDF, image, and video files are stored in PostgreSQL. Pasted images become real attachments, and image metadata (EXIF) is stripped. |
| Bulk actions | Change status, priority, or environment — or delete — across many items at once. |
| Reports | A report builder (manager/admin) that exports a multi-sheet Excel file. |
| Notifications | In-app bell, email (per event or one daily digest), and optional browser/FCM push. |
| Audit log | Every create, update, delete, and login is recorded for admins and managers. Entries stay even after an item is deleted. |
| Sessions | Admins see every active session (user, role, IP, browser, time) and can log out a single device. |
| Login | Local accounts with bcrypt-hashed passwords, three roles (admin / manager / user), and email password reset. |
| Sleuth assistant | Answers plain-English questions and runs actions (with confirmation). Runs locally by default; see [Sleuth](#sleuth). |
| UI | Light and dark themes, responsive layout, auto-refresh. |

### Roles

- **Admin** — full access, including user management and all deletes.
- **Manager** — edit any item or event, but cannot delete, grant the admin role, or edit existing admins.
- **User** — create and edit bugs only; tasks and requirements are read-only.

Deleting items, editing/deleting comments, and deleting attachments are
admin-only for every type.

## Architecture

- **Backend** — FastAPI, SQLAlchemy 2.x, Pydantic 2. PostgreSQL 16 in production; SQLite for tests and quick local runs.
- **Frontend** — React 18 + TypeScript, built with Vite into `app/static`, which FastAPI serves directly.
- **Packaging** — Docker Compose runs the app and its own PostgreSQL. The image is built on `python:3.12-slim`.
- **Sleuth** — pure-Python rules and a TF-IDF classifier, plus an optional local LLM and an optional cloud LLM.

```
app/
├── config.py · database.py · main.py · models.py · schemas.py
├── auth.py · email_service.py · notification_service.py · push_service.py
├── routes/      # auth · users · projects · bugs · events · stats
│                # audit · sessions · reports · notifications · push
├── chatbot/     # Sleuth: nlu · classifier · llm · cloud_llm · redaction
│                # rag · retrieval · verify · agent · evals (cloud grounding)
│                # executor · actions · memory · excel · ingest · router
├── jobs/        # email_digest (scheduled digest job)
└── static/      # built React bundle
frontend/        # React + TypeScript SPA source (Vite) → builds into app/static
tests/           # SQLite-backed pytest suite
models/          # GGUF files for Sleuth's optional local LLM (gitignored)
```

## Quick start

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Docker Engine + Compose v2).

```bash
git clone https://github.com/<your-org>/bug-hunter.git
cd bug-hunter
cp .env.example .env       # edit the values you care about — see Configuration
./deploy.sh
```

`./deploy.sh` builds the image, starts PostgreSQL, waits for it to be healthy,
then starts the app. Open <http://localhost:8765>.

PostgreSQL runs in its own container on host port `55432` (an unusual port, to
avoid clashing with any PostgreSQL you already run). Data lives in the named
volume `bugtracker_pgdata`, which `./deploy.sh` and `./down.sh` never delete (see
[Live-data safety](#live-data-safety)).

For a clean rebuild, run `BUILD_CLEAN=1 ./deploy.sh`. Behind a corporate proxy or
air gap, set `BASE_IMAGE` in `.env` to an internal mirror, or pre-load
`python:3.12-slim` with `docker save | docker load`.

### First login

On an empty database, Bug Hunter creates a first admin from the
`BOOTSTRAP_ADMIN_*` variables (default `admin@bughunter.local` / `ChangeMe123!`).
Change this password right away from the profile menu. A production deploy
(`COOKIE_SECURE=true`) refuses to start if you leave the default password.

### Production checklist

```bash
SESSION_SECRET=$(openssl rand -hex 32)    # required in prod; never blank
COOKIE_SECURE=true                        # only when serving over HTTPS
BOOTSTRAP_ADMIN_EMAIL=you@example.com
BOOTSTRAP_ADMIN_PASSWORD=<a strong password>
APP_BASE_URL=https://bugs.example.com
CORS_ORIGINS=https://bugs.example.com
```

Then `./down.sh && ./deploy.sh`.

### Stopping

```bash
./down.sh                  # stop containers; keep database volume and image
./down.sh --wipe-db        # also delete the database volume (asks for confirmation)
./down.sh --remove-images  # also remove the built image
./down.sh --full-clean     # both
```

## Configuration

All settings come from environment variables. Copy [`.env.example`](.env.example)
to `.env` and edit it — every variable is explained inline in that file and read
in [`app/config.py`](app/config.py). The ones that matter most:

| Variable | Default | Purpose |
|---|---|---|
| `SESSION_SECRET` | _(blank)_ | Signs session cookies. Set a long random value in production (`openssl rand -hex 32`). Blank makes a new secret every restart, which logs everyone out. |
| `COOKIE_SECURE` | `false` | Set `true` only when serving over HTTPS. |
| `ENABLE_API_DOCS` | `false` | API docs (`/docs`, `/redoc`) are always on in development. A production deploy (`COOKIE_SECURE=true`) turns them off unless this is `true`. |
| `APP_BASE_URL` | `http://localhost:8765` | Public URL used in email links. |
| `CORS_ORIGINS` | _(blank = same-origin)_ | Comma-separated list of allowed cross-origin clients. |
| `BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` / `_NAME` | `admin@bughunter.local` / `ChangeMe123!` / `Admin` | First admin (only created when the database has no users). |
| `EMAIL_BACKEND` | `console` | `console` (log to stdout), `smtp`, or `disabled`. |
| `EMAIL_DIGEST_ENABLED` | `false` | Batch per-event emails into one daily digest. |
| `MAX_REPORT_ROWS` | `50000` | Max rows in one Reports Excel export (returns 413 above it). |
| `WEB_PUSH_ENABLED` | `false` | Master switch for browser push (FCM). |
| `SLEUTH_CLOUD_ENABLED` | `0` | Opt-in cloud LLM fallback for Sleuth. |

Settings are grouped into: login and sessions, password policy, email and SMTP,
the daily digest (and its optional built-in scheduler), reports, web push
(Firebase), and the Sleuth assistant. See `.env.example` for the full list.

Secrets (`.env`, `secrets/firebase-admin.json`) are gitignored and set up
per server — never commit real secrets.

### Email (optional)

By default, `EMAIL_BACKEND=console` just prints emails to the log. For real
delivery, set `EMAIL_BACKEND=smtp` and the `SMTP_*` variables (host, port,
username, password, TLS), then restart. Any standard SMTP provider works.

Set `EMAIL_DIGEST_ENABLED=true` to batch each user's notifications (new item,
update, assignment, comment, event) into one email per day instead of sending
each one right away. Password-reset and other security emails always send
immediately and are never batched. Run the digest from cron or Task Scheduler:

```bash
python -m app.jobs.email_digest
```

Or set `EMAIL_DIGEST_CRON` (a 5-field cron expression) plus
`EMAIL_DIGEST_TIMEZONE` and the app runs the digest itself. The job is safe to
re-run and bounded to its time window, so you can leave it scheduled whether or
not the digest is on.

### Web push (optional)

Browser push uses Firebase Cloud Messaging and is off by default. One-time setup:

1. Create or reuse a project at <https://console.firebase.google.com>.
2. Add a Web app and copy its config into `FIREBASE_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_PROJECT_ID`, `FIREBASE_MESSAGING_SENDER_ID`, and `FIREBASE_APP_ID`.
3. Generate a Web Push key pair and put the public key in `FIREBASE_VAPID_KEY`.
4. Download a service-account key and save it as `secrets/firebase-admin.json`. `docker-compose.yml` mounts it read-only and points `FCM_CREDENTIALS_FILE` at it.
5. Set `WEB_PUSH_ENABLED=true`, restart, and serve over HTTPS (browsers only allow push on a secure origin; `localhost` is exempt for development).

Each user then turns on push once from the profile menu. The Firebase SDK is
self-hosted (no CDN), and the feature adds just one table.

## Local development

### Backend

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
python -m uvicorn app.main:app --reload    # http://127.0.0.1:8000
```

With no `DATABASE_URL` set, the app uses a local SQLite file, so you can run the
backend without Docker.

### Frontend

The SPA source is in `frontend/`. The build writes the static bundle into
`app/static/`, which FastAPI serves — so after editing the frontend, rebuild and
reload the app.

```bash
cd frontend
npm install
npm run build          # type-checks, vendors the Firebase SDK, writes app/static
```

`npm run dev` runs the Vite dev server for fast edits; `npm run build` produces
the production bundle.

## Testing

The suite is self-contained — each test file uses its own temporary SQLite
database and never touches production data.

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

## Deployment

To deploy in production: `git pull`, then `./deploy.sh`. There is no separate
migration step — `init_db()` brings the schema up to date on boot, adding only
what's missing. Secrets (`.env` and `secrets/firebase-admin.json`) are set up
once per server and never committed.

## Live-data safety

`./deploy.sh` rebuilds the image and restarts the stack without touching the
`bugtracker_pgdata` volume. `./down.sh` (no flags) stops the containers and keeps
the volume. You can only lose data through explicit, opt-in commands:
`./down.sh --wipe-db` (which asks for confirmation), `docker compose down -v`, or
deleting the volume by hand.

Upgrades are always additive. On boot, `init_db()` creates any missing tables and
columns via `Base.metadata.create_all()` and never changes existing rows. Sleuth
adds no tables and changes no columns — read intents only `SELECT`, and write
intents go through the same audited paths as the REST API.

## Sleuth

Sleuth is the in-app assistant — a floating widget on every page (open it with
`Ctrl + /` or `⌘ + /`).

**Ask questions** (answered by exact SQL handlers):

- *show open bugs assigned to alice*
- *how many critical bugs are in PROD?*
- *bugs created in the last 7 days*
- *export all bugs in apollo to excel* (returns a real `.xlsx`)
- *report of who solved how many bugs last week*

**Run actions** (always confirmed before any change, always audited):

- *close bug 5* · *reopen #12* · *mark #7 as resolved*
- *assign bug 3 to alice* · *set bug 9 priority to high*
- *comment on #5: looks fixed in v2.1*
- *create a bug titled "Login broken" in project Apollo*

After a turn that names a bug, pronouns (*close it*) work for a short window.

### How it works

Sleuth tries the cheapest layer first and only moves up when needed:

1. **Rules** (`app/chatbot/nlu.py`) — a regex parser over verbs, filters, names, and IDs. Handles most queries.
2. **Statistical classifier** (`app/chatbot/classifier.py`) — TF-IDF and cosine similarity over a curated corpus, no external models. Catches reworded questions.
3. **Local LLM** (`app/chatbot/llm.py`) — optional, lazily loaded `llama.cpp` against a GGUF model in `models/`. Used only when layers 1 and 2 are unsure. See [models/README.md](models/README.md).
4. **Cloud LLM** (`app/chatbot/cloud_llm.py`) — optional and off by default. When `SLEUTH_CLOUD_ENABLED=1` and a key is set, free-form questions can fall through to Gemini (primary) or OpenRouter (fallback), optionally with RAG retrieval.

With the defaults (`SLEUTH_CLOUD_ENABLED=0`, no model file), Sleuth is fully
local: no outbound HTTP, no telemetry, no third-party API. The cloud layer is the
only path that sends text off the box, and even then all text first passes
through a secret-redaction filter (`app/chatbot/redaction.py`). In every mode,
Sleuth never writes data through the model and never invents counts.

When the cloud layer is on, four read-only add-ons can each be turned on
separately to make answers more accurate and trustworthy. All are off by default
and need no extra dependencies, so they fit a small box:

- **Grounding** (`SLEUTH_RETRIEVAL_ENABLED`) — answers free-form questions from real bug records found by keyword search, with no vector database.
- **Agent** (`SLEUTH_AGENT_ENABLED`) — for a multi-step question, the model runs a few read-only lookups before answering. Every lookup goes through the same write firewall, so the agent can never change data.
- **Verification** (`SLEUTH_VERIFY_ANSWERS`) — checks every bug number an answer cites against the records and flags any that aren't backed by data. No extra model call.
- **Evaluation** (`SLEUTH_EVAL_ENABLED`) — a second model call scores each answer for grounding and adds a short "please verify" note when confidence is low. It can only annotate, never rewrite.

Key cloud-layer variables (all off or blank by default): `SLEUTH_CLOUD_ENABLED`,
`GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-2.5-flash`),
`OPENROUTER_API_KEY`, `SLEUTH_RAG_ENABLED`, `SLEUTH_RETRIEVAL_ENABLED`,
`SLEUTH_VERIFY_ANSWERS`, `SLEUTH_AGENT_ENABLED`, and `SLEUTH_EVAL_ENABLED`. The
`/api/chat` endpoint is rate-limited to 30 messages per minute per user.

## Security

See [SECURITY.md](SECURITY.md) for supported versions, how to report a
vulnerability privately, and a summary of the security posture.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run the test suite before opening a pull
request. Report vulnerabilities privately via [SECURITY.md](SECURITY.md), not as
public issues.

## License

[MIT](LICENSE.txt).
