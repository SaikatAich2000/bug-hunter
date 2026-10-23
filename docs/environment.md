<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Key settings, email, push and telemetry

The settings that matter most.

> [← Back to the README](../README.md)

## Configuration

All settings come from environment variables. Copy [`.env.example`](../.env.example)
to `.env`. It holds one short block per feature (a heading and the variables to set or switch on); every other
setting keeps its built-in default, read in [`app/config.py`](../app/config.py). On Render, open the service's Environment tab, choose Add from .env and paste the file, then change `APP_ENV`, `APP_BASE_URL`, `DATABASE_URL`, `COOKIE_SECURE`, `TRUST_PROXY_FORWARDED_FOR` and the `SMTP_*` values as the first lines of the file say. Never commit `.env`. Secret values belong in
`.env` (local), a Compose `--env-file`, or a platform secret store, never in the
image or the repository. The ones that matter most:

| Variable | Default | Purpose |
|---|---|---|
| `APP_VERSION` | _(required)_ | Release shown in the UI, API docs and `/api/health`, and the image tag. The single source of truth; there's no hardcoded fallback. |
| `APP_NAME` | `Bug Hunter` | Product name in HTML titles, API docs, email subjects, reports and notifications. |
| `APP_ENV` | _(blank = development)_ | `production` turns on the strict start-up checks. |
| `SESSION_SECRET` | _(blank)_ | Signs session cookies. Blank makes a new secret every restart, which logs everyone out, and is refused in production. |
| `COOKIE_SECURE` | `false` | Set `true` only when serving over HTTPS. Also counts as a production deploy. |
| `APP_BASE_URL` | `http://localhost:8765` | Public URL used in email links. |
| `CORS_ORIGINS` | _(blank = same-origin)_ | Comma-separated list of allowed cross-origin clients. |
| `ENABLE_API_DOCS` | `false` | Serve `/docs`, `/redoc` and `/openapi.json` in production too. They are always on in development. |
| `BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` / `_NAME` | `admin@bughunter.local` (Compose) / _(required)_ / `Admin` | First admin, created only when the database has no users. |
| `AUTO_LOGIN_ENABLED` | `false` | Sign every visitor in as the bootstrap admin. Trusted local use only. |
| `EMAIL_BACKEND` | `console` | `console` (log to stdout), `smtp`, or `disabled`. |
| `EMAIL_DIGEST_ENABLED` | `false` | Batch per-event emails into one daily digest. |
| `MAX_REPORT_ROWS` | `50000` | Max rows in one Reports Excel export (returns 413 above it). |
| `WEB_PUSH_ENABLED` | `false` | Master switch for browser push (FCM). |
| `GIT_BRANCH_CREATION_ENABLED` / `GIT_BRANCH_DELETION_ENABLED` | `false` / `false` | Story feature branches; see [Git integration](git-integration.md#git-integration-story-feature-branches). |
| `GIT_CREDENTIAL_ENCRYPTION_KEY` | _(blank)_ | Fernet key that encrypts per-project Git tokens. Required before a token can be saved. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(blank)_ | OTLP/**gRPC** collector, e.g. `http://otel-collector:4317`. Blank means console-only, with nothing exported. |
| `LOG_FORMAT` | `text` | `json` writes one JSON object per line for log shippers. |
| `SLEUTH_CLOUD_ENABLED` | `0` | Opt-in cloud LLM fallback for Sleuth. |

### Observability (OpenTelemetry)

Bug Hunter can send **MELT** (metrics, events, logs and traces) to SigNoz or any OpenTelemetry backend. It is off until you set
`OTEL_EXPORTER_OTLP_ENDPOINT`, so connecting a backend later takes only a few lines in `.env`. The full guide, with every setting and
the SigNoz steps, is in [Observability](observability.md).

### Email (optional)

By default, `EMAIL_BACKEND=console` prints emails to the log. For real delivery,
set `EMAIL_BACKEND=smtp` and the `SMTP_*` variables (host, port, username,
password, TLS), then restart. Any standard SMTP provider works. For Gmail, use an
[App Password](https://myaccount.google.com/apppasswords), not the account
password.

Set `EMAIL_DIGEST_ENABLED=true` to batch each user's notifications into one
email per day. Password-reset and other security emails always send immediately.
With the digest on, immediate work-item emails are off, so make sure a scheduler
runs. Either run `python -m app.jobs.email_digest` from cron/Task Scheduler, or
set `EMAIL_DIGEST_CRON` (5-field cron) and `EMAIL_DIGEST_TIMEZONE` (IANA name) to
let the app run it itself (the log confirms with `Email-digest scheduler started`).
The job is idempotent, bounded by `EMAIL_DIGEST_LOOKBACK_HOURS` (default 50), and
retries failed sends on the next run.

### Web push (optional)

Browser push uses Firebase Cloud Messaging and is off by default. One-time setup:

1. Create or reuse a project at <https://console.firebase.google.com>.
2. Add a Web app and copy its config into `FIREBASE_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_PROJECT_ID`, `FIREBASE_MESSAGING_SENDER_ID` and `FIREBASE_APP_ID`.
3. Generate a Web Push key pair and put the public key in `FIREBASE_VAPID_KEY`.
4. Provide a service-account key in one of two ways:
   - **File mount** (default Compose setup): save it as `secrets/firebase-admin.json` (`FCM_CREDENTIALS_FILE`).
   - **Env var** (no mount, e.g. Azure Container Apps): put the JSON, raw or base64-encoded, in `FCM_CREDENTIALS_JSON` as a platform secret. It takes priority over the file.
5. Set `WEB_PUSH_ENABLED=true`, restart, and serve over HTTPS (`localhost` is exempt).

Each user then enables push once from the profile menu. The Firebase SDK is
self-hosted (no CDN). If a token fails to register, the client retries with
backoff and again on reconnect. Devices still need to reach Google's FCM
endpoints, and on networks that block them push never arrives. The in-app bell
doesn't depend on FCM.


## Other settings

`.env` lists only what must be set or switched on. Every setting below has a working default; add a line to `.env` (or a Render variable) only to change it.


### Sleuth AI

| Setting | Default | What it does |
|---|---|---|
| `GROQ_MODEL` | openai/gpt-oss-120b | Groq chat model |
| `OPENROUTER_API_KEY` | empty | Fallback model provider when no Groq key is set |
| `OPENROUTER_MODEL` | qwen/qwen-2.5-7b-instruct:free | Model used with OpenRouter |
| `GEMINI_EMBED_MODEL` | gemini-embedding-001 | Embedding model for document search (chat itself never calls Gemini) |
| `SLEUTH_CLOUD_MAX_TOKENS` | 1024 | Longest cloud reply, in tokens |
| `SLEUTH_CLOUD_TEMPERATURE` | 0.6 | Randomness of conversational replies |
| `SLEUTH_CLOUD_TEMPERATURE_TOOLS` | 0.0 | Randomness for tool and evaluation calls |
| `SLEUTH_CLOUD_FREQUENCY_PENALTY` | 0.3 | Discourages repeated words |
| `SLEUTH_CLOUD_PRESENCE_PENALTY` | 0.2 | Encourages new topics |
| `SLEUTH_CLOUD_COOLDOWN_S` | 45 | Pause after the cloud model fails before trying it again |
| `SLEUTH_ANSWER_MAX_CHARS` | 4000 | Character limit on one answer |
| `SLEUTH_RAG_TOP_K` | 5 | Document passages given to the model per question |
| `SLEUTH_RAG_DIR` | .sleuth_rag | Where the document index is stored |
| `SLEUTH_AGENT_MAX_STEPS` | 4 | Steps the read-only agent may take per question |
| `SLEUTH_LLM_TOOLS_MAX_ROUNDS` | 4 | Tool-call rounds per question |
| `SLEUTH_EVAL_MIN_SCORE` | 0.5 | Score below which an answer gets a caveat appended |
| `SLEUTH_CHAT_MEMORY_ENABLED` | true | Remember earlier messages in a conversation |
| `SLEUTH_MEMORY_MAX_SESSIONS` | 200 | Conversations kept in memory |
| `SLEUTH_MEMORY_TTL_SECONDS` | 1800 | Idle time before a conversation is forgotten |
| `SLEUTH_CONFIRM_TTL_SECONDS` | 300 | How long a confirmation prompt stays valid |
| `SLEUTH_EXPORT_TTL_SECONDS` | 1800 | How long a generated export stays downloadable |
| `SLEUTH_EXPORT_MAX_ENTRIES` | 50 | Exports kept at once |
| `SLEUTH_BULK_ACTION_CAP` | 500 | Most items one bulk action may change |
| `SLEUTH_INGEST_MAX_BYTES` | 5 MiB | Largest document that can be added to the index |

### Sleuth local model (optional)

| Setting | Default | What it does |
|---|---|---|
| `SLEUTH_LLM_MODEL_PATH` | models/sleuth.gguf | Optional local llama.cpp model; unused when the file is absent |
| `SLEUTH_LLM_CTX_LEN` | 1024 | Local model context size |
| `SLEUTH_LLM_THREADS` | 1 | Local model CPU threads |
| `SLEUTH_LLM_MAX_TOKENS` | 120 | Longest local reply, in tokens |
| `SLEUTH_LLM_TIMEOUT_S` | 12 | Seconds before the local model is abandoned |
| `SLEUTH_LLM_IDLE_UNLOAD_S` | 600 | Idle seconds before the local model is unloaded |
| `SLEUTH_LLM_RAM_HEADROOM` | 1.4 | Memory needed as a multiple of the model file size |

### Git integration

| Setting | Default | What it does |
|---|---|---|
| `GITHUB_DEFAULT_BASE_BRANCH` | dev | Branch new branches start from |
| `GITHUB_CONNECT_TIMEOUT_SECONDS` | 5 | Connect timeout for GitHub calls |
| `GITHUB_READ_TIMEOUT_SECONDS` | 15 | Read timeout for GitHub calls |
| `GITHUB_MAX_RETRIES` | 2 | Retries for temporary GitHub failures (never for auth or validation errors) |
| `GITHUB_RETRY_BACKOFF_SECONDS` | 0.5 | Wait between GitHub retries |
| `GITHUB_BRANCH_TITLE_MAX_LENGTH` | 25 | Characters of the work item title used in a branch name |
| `GITHUB_BRANCH_NAME_MAX_LENGTH` | 120 | Longest generated branch name |
| `GIT_ALLOW_PRIVATE_HOSTS` | false | Let the Git server be on a private address (on-premises GitHub Enterprise) |

### Mail, webhooks, database, telemetry

| Setting | Default | What it does |
|---|---|---|
| `SMTP_USE_SSL` | false | Use SMTPS (port 465) instead of STARTTLS |
| `DB_CONNECT_TIMEOUT_SECONDS` | 10 | Seconds to wait when connecting to the database at start-up |
| `OTEL_EXPORTER_OTLP_COMPRESSION` | none | OpenTelemetry standard: gzip or none |

### Limits and cleanup

| Setting | Default | What it does |
|---|---|---|
| `IMPORT_MAX_ROWS` | 1000 | Most rows in one spreadsheet import |
| `IMPORT_MAX_FILE_BYTES` | 10 MiB | Largest import file |
| `MAX_ASSET_FILE_BYTES` | 8 MiB | Largest logo or image upload |
| `PAGE_SIZE_DEFAULT` | 50 | Items per page when the client does not say |
| `PAGE_SIZE_MAX` | 200 | Most items per page a client may ask for |
| `LAST_SEEN_THROTTLE_SECONDS` | 60 | Minimum gap between last-seen updates per user |
| `RETENTION_BATCH_SIZE` | 1000 | Rows deleted per batch by the cleanup job |
| `RETENTION_FIRST_DELAY_SECONDS` | 300 | Wait after start before the first cleanup |
| `RETENTION_INTERVAL_SECONDS` | 86400 | Time between cleanups |

### Running locally and seeding demo data

| Setting | Default | What it does |
|---|---|---|
| `UVICORN_HOST` | 127.0.0.1 | Address `python -m app.main` listens on (the container image binds 0.0.0.0 itself) |
| `UVICORN_PORT` | 8000 | Port `python -m app.main` listens on |
| `VITE_DEV_PORT` | 5173 | Port of `npm run dev` (the frontend dev server) |
| `VITE_DEV_API_TARGET` | http://localhost:8765 | Backend `npm run dev` forwards /api requests to |
| `CONTAINER_APP_NAME` | empty | Azure Container Apps: app name, used to work out the public address |
| `CONTAINER_APP_HOSTNAME` | empty | Azure Container Apps: public host name |
| `CONTAINER_APP_ENV_DNS_SUFFIX` | empty | Azure Container Apps: environment DNS suffix |
| `SEED_BASE_URL` | APP_BASE_URL | Server `scripts/seed_local.py` fills with demo data |
| `SEED_EMAIL_DOMAIN` | example.test | E-mail domain of the demo users |
| `SEED_USER_PASSWORD` | the admin password | Password of the demo users |
