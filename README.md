# Bug Hunter

Bug Hunter is a self-hosted work-item tracker built on FastAPI and PostgreSQL with a React + TypeScript single-page frontend. It runs as a single Docker Compose stack, requires no external authentication provider, and stores attachments in the database, so a single database backup captures everything. Bugs, requirements, and tasks share one numbering scheme; projects, events, comments, an audit trail, notifications, reports, and an optional in-app assistant ("Sleuth") complete the toolset.

Current version: **3.0**. Schema migrations are strictly additive, so an existing production database is left intact on upgrade (see [Live-data safety](#live-data-safety)).

A companion native Android app lives in a separate repository and registers into the same web-push table.

## Features

| Area | What it does |
|---|---|
| Work items | Bugs, requirements, and tasks share one `#N` counter. The tab strip (All / Bugs / Requirements / Tasks) scopes KPIs, filters, columns, and analytics to the active type. Each type has its own status set, and bugs additionally carry a DEV/UAT/PROD environment. |
| Projects & events | Projects group work; events group items for a standup or sprint, each with one or more managers. |
| Item links | Directed relationships between items: relates, blocks, duplicate. |
| Comments & attachments | Rich-text descriptions and comments (bold, italic, lists, code, blockquote). PDF, image, and video attachments are stored as PostgreSQL blobs. Pasted images upload as real attachments; uploaded image metadata (EXIF) is stripped. |
| Bulk actions | Change status, priority, or environment, or delete, across many selected items in one request. |
| Reports | Report builder (manager/admin) with multi-sheet XLSX export. |
| Notifications | Per-user in-app bell, per-operation or daily-digest email, and optional browser/FCM web push. |
| Audit log | Every create, update, delete, and login is recorded for admins and managers; entries survive item deletion. |
| Sessions | Admins list every active session (user, role, IP, browser, timestamps) and can revoke a single device. |
| Authentication | Local login with bcrypt-hashed passwords, role-based access (admin / manager / user), and an emailed password-reset flow. |
| Sleuth assistant | In-app assistant that answers natural-language questions and runs audited actions. Local by default; see [Sleuth](#sleuth). |
| UI | Light and dark themes, responsive layout, auto-refresh. |

### Roles

- **Admin** — full access, including user management and all deletes.
- **Manager** — edit any item or event; cannot delete, grant the admin role, or edit existing admins.
- **User** — create and edit bugs only; tasks and requirements are read-only.

Deletion, comment edit/delete, and attachment delete are admin-only across every type.

## Architecture

- **Backend** — FastAPI, SQLAlchemy 2.x, Pydantic 2. PostgreSQL 16 in production; SQLite for tests and quick local runs.
- **Frontend** — React 18 + TypeScript built with Vite into `app/static`, which FastAPI serves directly.
- **Packaging** — Docker Compose runs the app and an isolated PostgreSQL instance; the image is based on `python:3.12-slim`.
- **Sleuth** — in-process rules and a TF-IDF classifier (pure Python), with an optional local LLM and an optional cloud LLM.

```
app/
├── config.py · database.py · main.py · models.py · schemas.py
├── auth.py · email_service.py · notification_service.py · push_service.py
├── routes/      # auth · users · projects · bugs · events · stats
│                # audit · sessions · reports · notifications · push
├── chatbot/     # Sleuth: nlu · classifier · llm · cloud_llm · rag · redaction
│                # executor · actions · memory · excel · router
├── jobs/        # email_digest (scheduled digest job)
└── static/      # built React bundle
frontend/        # React + TypeScript SPA source (Vite) → builds into app/static
tests/           # SQLite-backed pytest suite
models/          # GGUF files for Sleuth's optional local LLM (gitignored)
```

## Quick start

Prerequisite: [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Docker Engine + Compose v2).

```bash
git clone https://github.com/<your-org>/bug-hunter.git
cd bug-hunter
cp .env.example .env       # edit the values you care about — see Configuration
./deploy.sh
```

`./deploy.sh` builds the image, starts PostgreSQL, waits for it to be healthy, then starts the app. Open <http://localhost:8765>.

PostgreSQL runs in its own container on host port `55432` (non-standard to avoid clashing with a local PostgreSQL). Data lives in the named volume `bugtracker_pgdata` and is never removed by `./deploy.sh` or `./down.sh` (see [Live-data safety](#live-data-safety)).

For a forced clean rebuild, run `BUILD_CLEAN=1 ./deploy.sh`. Behind a corporate proxy or air gap, set `BASE_IMAGE` in `.env` to an internal mirror, or pre-load `python:3.12-slim` with `docker save | docker load`.

### First login

On an empty database, a bootstrap admin is created from the `BOOTSTRAP_ADMIN_*` variables (default `admin@bughunter.local` / `ChangeMe123!`). Change the password immediately from the profile menu. A production deploy (`COOKIE_SECURE=true`) refuses to start with the built-in default password.

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

All configuration is read from environment variables. Copy [`.env.example`](.env.example) to `.env` and edit it; the file documents every variable inline and is parsed in [`app/config.py`](app/config.py). The most important settings:

| Variable | Default | Purpose |
|---|---|---|
| `SESSION_SECRET` | _(blank)_ | Signs session cookies. Set a long random value in production (`openssl rand -hex 32`); blank generates a new secret per restart, logging everyone out. |
| `COOKIE_SECURE` | `false` | Set `true` only when serving over HTTPS. |
| `APP_BASE_URL` | `http://localhost:8765` | Public URL used in email links. |
| `CORS_ORIGINS` | _(blank = same-origin)_ | Comma-separated allow-list of cross-origin clients. |
| `BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` / `_NAME` | `admin@bughunter.local` / `ChangeMe123!` / `Admin` | First-run admin (only when the database has no users). |
| `EMAIL_BACKEND` | `console` | `console` (log to stdout), `smtp`, or `disabled`. |
| `EMAIL_DIGEST_ENABLED` | `false` | Batch per-operation emails into one daily digest. |
| `MAX_REPORT_ROWS` | `50000` | Row ceiling for one Reports XLSX export (413 above it). |
| `WEB_PUSH_ENABLED` | `false` | Master switch for browser push (FCM). |
| `SLEUTH_CLOUD_ENABLED` | `0` | Opt-in cloud LLM fallback for Sleuth. |

Configuration groups: authentication and session, password policy, email and SMTP, daily digest and its optional in-app scheduler, reports, web push (Firebase), and the Sleuth assistant. See `.env.example` for the full list.

Secrets (`.env`, `secrets/firebase-admin.json`) are gitignored and provisioned per host; never commit real secrets.

### Email (optional)

By default `EMAIL_BACKEND=console` logs emails to stdout. For real delivery, set `EMAIL_BACKEND=smtp` and the `SMTP_*` variables (host, port, username, password, TLS), then restart. Any standard SMTP provider works.

Set `EMAIL_DIGEST_ENABLED=true` to batch each user's per-operation notifications (new item, update, assignment, comment, event) into one grouped email per day instead of sending each immediately. Password-reset and other security emails always send immediately and are never batched. Run the digest from host cron or Task Scheduler:

```bash
python -m app.jobs.email_digest
```

Or set `EMAIL_DIGEST_CRON` (a 5-field cron expression) plus `EMAIL_DIGEST_TIMEZONE` and the app schedules the digest itself. The job is idempotent and window-bounded, so it is safe to leave scheduled whether or not the digest is enabled.

### Web push (optional)

Browser push uses Firebase Cloud Messaging and is off by default. One-time setup:

1. Create or reuse a project at <https://console.firebase.google.com>.
2. Add a Web app and copy its config into `FIREBASE_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_PROJECT_ID`, `FIREBASE_MESSAGING_SENDER_ID`, and `FIREBASE_APP_ID`.
3. Generate a Web Push key pair and put the public key in `FIREBASE_VAPID_KEY`.
4. Download a service-account key and save it as `secrets/firebase-admin.json`; `docker-compose.yml` mounts it read-only and points `FCM_CREDENTIALS_FILE` at it.
5. Set `WEB_PUSH_ENABLED=true`, restart, and serve over HTTPS (browsers allow push only on a secure origin; `localhost` is exempt for development).

Each user then enables push once from the profile menu. The Firebase SDK is self-hosted (no CDN), and the feature adds one table only.

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

With no `DATABASE_URL` set, the app uses a local SQLite file, so the backend runs without Docker.

### Frontend

The SPA source lives in `frontend/`. The build emits the static bundle into `app/static/`, which FastAPI serves, so after editing the frontend you rebuild and reload the app.

```bash
cd frontend
npm install
npm run build          # type-checks, vendors the Firebase SDK, writes app/static
```

`npm run dev` runs the Vite dev server for a fast inner loop; `npm run build` produces the production-equivalent bundle.

## Testing

The suite is hermetic — each test file uses its own temporary SQLite database and never touches production data.

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

SonarQube configuration is in `sonar-project.properties` (see `scripts/sonar-scan.{sh,ps1}`). Static analysis only; it never touches the runtime database.

## Deployment

Production deployment is `git pull` followed by `./deploy.sh`. There is no separate migration step — `init_db()` reconciles the schema additively on boot. Secrets (`.env` and `secrets/firebase-admin.json`) are provisioned once per host and are never committed.

## Live-data safety

`./deploy.sh` rebuilds the image and restarts the stack without touching the `bugtracker_pgdata` volume. `./down.sh` (no flags) stops the containers and leaves the volume intact. Data loss is only possible through explicit opt-in actions: `./down.sh --wipe-db` (which requires confirmation), `docker compose down -v`, or manually deleting the volume.

Schema migrations are strictly additive. Every upgrade is idempotent against an existing database: new tables and columns are created by `init_db()` on boot via `Base.metadata.create_all()`, and existing rows are never modified. Sleuth adds no tables and modifies no columns — read intents only `SELECT`, and write intents go through the same audited paths as the REST API.

## Sleuth

Sleuth is the in-app assistant — a floating widget on every page (open with `Ctrl + /` or `⌘ + /`).

**Ask questions** (answered by deterministic SQL handlers):

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

After a turn that names a bug, pronouns (*close it*) resolve for a short window.

### How it works

Sleuth tries the cheapest layer first and escalates only when needed:

1. **Rules** (`app/chatbot/nlu.py`) — a regex classifier over verbs, filters, names, and IDs. Handles most queries.
2. **Statistical classifier** (`app/chatbot/classifier.py`) — TF-IDF and cosine similarity over a curated corpus, no external models. Catches paraphrases.
3. **Local LLM** (`app/chatbot/llm.py`) — optional, lazy-loaded `llama.cpp` against a GGUF model in `models/`, used only when layers 1 and 2 are uncertain. See [models/README.md](models/README.md).
4. **Cloud LLM** (`app/chatbot/cloud_llm.py`) — optional and off by default. When `SLEUTH_CLOUD_ENABLED=1` and a key is set, free-form questions can fall through to Gemini (primary) or OpenRouter (fallback), optionally with RAG retrieval.

With the defaults (`SLEUTH_CLOUD_ENABLED=0`, no model file), Sleuth is fully local: no outbound HTTP, no telemetry, no third-party API. The cloud layer (layer 4) is the only path that sends text off the box, and even then all text is passed through a secret-redaction filter (`app/chatbot/redaction.py`) first. In every mode, Sleuth never writes data through the model and never invents counts.

Key cloud-layer variables (all default off or blank): `SLEUTH_CLOUD_ENABLED`, `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-2.5-flash`), `OPENROUTER_API_KEY`, and `SLEUTH_RAG_ENABLED`. The `/api/chat` endpoint is rate-limited to 30 messages per minute per user.

## Security

See [SECURITY.md](SECURITY.md) for the supported-versions policy, the private vulnerability-reporting process, and a summary of the security posture.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run the test suite before submitting a pull request. Report vulnerabilities privately via [SECURITY.md](SECURITY.md), not as public issues.

## License

[MIT](LICENSE.txt).
