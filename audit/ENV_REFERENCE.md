# Environment variable reference

Generated from every environment variable read under `app/`, plus `docker-compose.yml` and the comments in
`.env.example` (regenerate with `python scripts/gen_env_reference.py` after changing any of them). Configuration precedence: real process
environment first, then `.env` (loaded with python-dotenv, `override=False`), then the default
below. Docker Compose passes values from `.env` into the container through `${VAR}` interpolation.

Values marked *secret* must come from `.env` or a platform secret store and never be committed.
Production start-up validation (`app/main.py::_production_config_errors`) refuses weak or
placeholder values for the settings marked required in production.

## Core

| Variable | Default | Required | Secret | Purpose |
|---|---|---|---|---|
| `APP_BASE_URL` | `""` | Yes with APP_ENV=production (https) |  | Direct Python uses this file as-is (port 8000). Docker Compose overrides it to the externally reachable port (see docker-compose.yml APP_BASE_URL): direct = http://127.0.0.1:8000, docker = http://localhost:8765. |
| `APP_ENV` | `""` | No |  | `production` (or `staging`) turns on the strict start-up checks; blank or `development` otherwise. |
| `APP_NAME` | `"Bug Hunter"` | No |  | Product name in page titles, API docs, emails, reports and notifications. |
| `APP_VERSION` | `""` | Yes (Compose refuses to start without it) |  | PRODUCT VERSION - THE SINGLE SOURCE OF TRUTH. There is no hardcoded copy anywhere in the code, Dockerfile, CI or release tooling: everything reads APP_VERSION from this file. Set it once per release; do not leave it blank (docker-compose.yml and the startup ch |
| `BASE_IMAGE` | `(compose)` | No |  | Base Python Docker image (for private registries, use: private-registry/python:3.12-slim) |
| `CORS_ORIGINS` | `""` | No |  | Cross-origin allow-list (comma-separated). Blank = same-origin only (secure default) For cross-origin clients, set: CORS_ORIGINS=https://example.com,https://other.com |
| `ENABLE_API_DOCS` | `False` | No |  | API docs (/docs, /redoc, /openapi.json) are always on in development. A production deploy (APP_ENV=production or COOKIE_SECURE=true) hides them unless this is true. |
| `LOG_FORMAT` | `"text"` | No |  | Console log format: text (default) or json (one JSON object per line) |
| `LOG_LEVEL` | `"INFO"` | No |  | Root log level (DEBUG, INFO, WARNING, ERROR). |
| `MAX_REQUEST_BODY_BYTES` | `60 * 1024 * 1024` | No |  | Maximum request body size (bytes; default 60 MiB) |
| `TRUST_PROXY_FORWARDED_FOR` | `False` | No |  | Trust X-Forwarded-For header only when behind a trusted reverse proxy |
| `TRUST_PROXY_HOP_COUNT` | `1` | No |  | Number of trusted proxies in front of the app: the client IP is the Nth X-Forwarded-For entry from the right (1 = a single reverse proxy). |

## Database

| Variable | Default | Required | Secret | Purpose |
|---|---|---|---|---|
| `DATABASE_URL` | `sqlite:///<repo>/bug_hunter.db` | Only without Compose (external PostgreSQL) |  | Override DATABASE_URL only for external databases (Azure, AWS RDS, an app-container deployment with no Compose, etc.) Format: postgresql+psycopg://user:password@host:port/dbname Leave unset for Docker Compose (auto-built from POSTGRES_* above) Leave unset for  |
| `DB_CONNECT_TIMEOUT_SECONDS` | `10` | No |  | Startup fail-fast guards (Postgres only; ignored for SQLite). Defaults are chosen for Azure Container Apps: a firewalled/slow database (or DDL waiting on another replica's lock) must fail fast instead of hanging the container at "Waiting for application startu |
| `DB_LOCK_TIMEOUT_MS` | `10000` | No |  | PostgreSQL `lock_timeout` per session, so a request never waits forever on a row lock. |
| `DB_MAX_OVERFLOW` | `10` | No |  | Extra PostgreSQL connections above DB_POOL_SIZE. Requests are admitted to (pool + overflow - 2) at once; the rest queue without blocking a worker thread. |
| `DB_POOL_SIZE` | `5` | No |  | PostgreSQL connection pool (ignored for SQLite) |
| `DB_STARTUP_TIMEOUT_SECONDS` | `60` | No |  | Overall budget for all blocking DB work during startup (init + reconcile). |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | No |  | PostgreSQL `statement_timeout` per session. |
| `POSTGRES_DB` | `(compose)` | No |  | Database name for the Compose PostgreSQL container (default bugtracker). |
| `POSTGRES_PASSWORD` | `(compose)` | Yes for Docker Compose | secret | Password for the Compose PostgreSQL container. Set before first boot; changing it later does not change the existing volume. |
| `POSTGRES_USER` | `(compose)` | No |  | User for the Compose PostgreSQL container (default bugtracker). |

## Authentication and sessions

| Variable | Default | Required | Secret | Purpose |
|---|---|---|---|---|
| `AUTO_LOGIN_ENABLED` | `False` | No |  | Auto-login convenience (skip login screen) ⚠️  SECURITY: Only enable on trusted local/dev deployments |
| `BOOTSTRAP_ADMIN_EMAIL` | `""` | Yes on a fresh database |  | Bootstrap admin — created on first run of an empty database. Both values are required on a fresh install; a production deploy refuses the placeholder. ⚠️  Change password immediately after first login via Profile menu |
| `BOOTSTRAP_ADMIN_NAME` | `"Admin"` | No |  | Display name of the first admin created on an empty database. |
| `BOOTSTRAP_ADMIN_PASSWORD` | `""` | Yes on a fresh database (Compose requires it) | secret | Password of the first admin (empty database only). Production refuses blank, `ChangeMe123!` and `replace_with...` placeholders. |
| `COOKIE_SECURE` | `False` | No |  | HTTPS enforcement (set to true in production with valid SSL certificate) |
| `FORGOT_PASSWORD_ENUMERATION_SAFE` | `True` | No |  | true = forgot-password always answers the same way, so it can't reveal which emails have accounts. |
| `PASSWORD_MIN_LENGTH` | `8` | No |  | Password policy |
| `PASSWORD_REQUIRE_COMPLEXITY` | `True` | No |  | Require at least one letter and one number in passwords. |
| `SESSION_REQUIRE_JTI` | `False` | No |  | Reject session cookies issued before per-device sessions existed (they can't be revoked individually). Leave false during an upgrade window from 3.x, then turn on. |
| `SESSION_SECRET` | `""` | Yes in production (>= 32 chars, not a placeholder) | secret | Session secret (generate with: python -c "import secrets; print(secrets.token_hex(32))") Blank = a random per-process secret (sessions reset on restart; dev only). A production deploy refuses to start without a real value. |
| `SESSION_TTL_SECONDS` | `86400` | No |  | Session TTL in seconds (86400 = 24 hours) |

## Email

| Variable | Default | Required | Secret | Purpose |
|---|---|---|---|---|
| `EMAIL_BACKEND` | `"console"` | No |  | Backend: "console" (log to stdout), "smtp" (real mail), or "disabled" |
| `EMAIL_DIGEST_CRON` | `""` | No |  | 5-field cron format: minute hour day month day-of-week Examples: "0 8 * * *" = every day at 8am; "0 8 * * 1-5" = weekdays 8am |
| `EMAIL_DIGEST_ENABLED` | `False` | No |  | Email Digest (batch notifications into one daily email) |
| `EMAIL_DIGEST_LOOKBACK_HOURS` | `50` | No |  | Lookback hours for digest (must be >= 2x gap between runs; daily = 50, weekly = 336) |
| `EMAIL_DIGEST_TIMEZONE` | `""` | No |  | Timezone for email digest (IANA name; "UTC" if blank) Examples: "America/New_York", "Asia/Kolkata", "Europe/London" |
| `EMAIL_FROM` | `"bughunter@localhost"` | No |  | From header for outgoing email. |
| `SMTP_HOST` | `""` | No |  | SMTP Configuration (used when EMAIL_BACKEND=smtp) |
| `SMTP_PASSWORD` | `""` | No | secret | SMTP password (Gmail: an App Password). |
| `SMTP_PORT` | `587` | No |  | SMTP port (587 with STARTTLS, 465 with SSL). |
| `SMTP_TIMEOUT` | `10` | No |  | SMTP connect/send timeout in seconds. |
| `SMTP_USERNAME` | `""` | No |  | SMTP login. |
| `SMTP_USE_SSL` | `False` | No |  | Implicit TLS (port 465). |
| `SMTP_USE_TLS` | `True` | No |  | STARTTLS (port 587). |

## Git integration

| Variable | Default | Required | Secret | Purpose |
|---|---|---|---|---|
| `GITHUB_API_URL` | `"https://api.github.com"` | No |  | Deployment DEFAULTS. A saved non-empty project value always wins over these, and these are never substituted into a project that already has a value. |
| `GITHUB_BRANCH_NAME_MAX_LENGTH` | `120` | No |  | Maximum generated branch name length. |
| `GITHUB_BRANCH_TITLE_MAX_LENGTH` | `25` | No |  | First 25 Unicode title characters used before safe slug creation (minimum 1). |
| `GITHUB_CONNECT_TIMEOUT_SECONDS` | `5.0` | No |  | TCP connect timeout for GitHub API calls. |
| `GITHUB_DEFAULT_BASE_BRANCH` | `"dev"` | No |  | Deployment default base branch; a project's own setting wins. |
| `GITHUB_MAX_RETRIES` | `2` | No |  | Bounded retries for transient GitHub failures. |
| `GITHUB_ORGANIZATION` | `""` | No |  | Deployment default organization; a project's own setting wins. |
| `GITHUB_READ_TIMEOUT_SECONDS` | `15.0` | No |  | Read timeout for GitHub API calls. |
| `GITHUB_TOKEN` | `""` | No | secret | Optional legacy fallback PAT (fine-grained). Never copied into the database, never returned to a browser, never logged or audited. |
| `GIT_BRANCH_CREATION_ENABLED` | `False` | No |  | Disabled by default. Two credential sources exist, in this order: 1. a per-project PAT saved in Project Git Settings (encrypted at rest), then 2. GITHUB_TOKEN below, a legacy deployment-wide fallback used only by projects that have no credential of their own.  |
| `GIT_BRANCH_DELETION_ENABLED` | `False` | No |  | Destructive remote branch removal is independently disabled by default. |
| `GIT_CA_BUNDLE_FILE` | `""` | No |  | Optional approved corporate CA bundle (PEM) for Linux containers behind TLS interception. Blank (default) = verify with the OS trust store via truststore. When set, point at a read-only mounted PEM file. A missing or invalid file fails closed; verification can |
| `GIT_CREDENTIAL_ENCRYPTION_KEY` | `""` | To save project Git tokens | secret | Server-only Fernet key protecting project Git credentials at rest. Required only to SAVE a project credential; without it, projects fall back to GITHUB_TOKEN. Generate once and keep it in the platform secret store: python -c "from cryptography.fernet import Fe |

## Web push

| Variable | Default | Required | Secret | Purpose |
|---|---|---|---|---|
| `FCM_CREDENTIALS_JSON` | `""` | No | secret | Method 2: Inline JSON (raw or base64-encoded; set if FCM_CREDENTIALS_FILE is empty) |
| `FIREBASE_API_KEY` | `""` | No | secret | Firebase Web SDK config (from console → Project Settings → General) |
| `FIREBASE_APP_ID` | `""` | No |  | Firebase web app config (console > Project settings > General). |
| `FIREBASE_AUTH_DOMAIN` | `""` | No |  | Firebase web app config. |
| `FIREBASE_MESSAGING_SENDER_ID` | `""` | No |  | Firebase web app config. |
| `FIREBASE_PROJECT_ID` | `""` | No |  | Firebase web app config. |
| `FIREBASE_VAPID_KEY` | `""` | No | secret | Public Web Push certificate key (Cloud Messaging > Web configuration). |
| `WEB_PUSH_ENABLED` | `False` | No |  | Master switch for browser push via Firebase Cloud Messaging. |

## Sleuth assistant

| Variable | Default | Required | Secret | Purpose |
|---|---|---|---|---|
| `GEMINI_API_KEY` | `""` | No | secret | Gemini embeddings key, used only by the optional RAG index (SLEUTH_RAG_ENABLED). |
| `GEMINI_EMBED_MODEL` | `"text-embedding-004"` | No |  | Embeddings model for RAG. |
| `GROQ_API_KEY` | `""` | No | secret | Primary provider: Groq (free tier, recommended) Get key: https://console.groq.com/keys |
| `GROQ_MODEL` | `"llama-3.3-70b-versatile"` | No |  | Groq chat model for Sleuth's cloud layer. |
| `OPENROUTER_API_KEY` | `""` | No | secret | Optional fallback provider: OpenRouter (supports multiple models) |
| `OPENROUTER_MODEL` | `"qwen/qwen-2.5-7b-instruct:free"` | No |  | OpenRouter fallback model. |
| `SLEUTH_AGENT_ENABLED` | `False` | No |  | Read-only multi-step agent: a few lookups before answering (never writes) |
| `SLEUTH_AGENT_MAX_STEPS` | `4` | No |  | Lookups the read-only agent may run per question. |
| `SLEUTH_ANSWER_MAX_CHARS` | `4000` | No |  | Cap on a cloud answer's length. |
| `SLEUTH_CHAT_MEMORY_ENABLED` | `True` | No |  | Remember the last few turns per user (pronouns like 'close it'). |
| `SLEUTH_CLOUD_ENABLED` | `False` | No |  | Cloud LLM layer (requires API key from a provider) |
| `SLEUTH_CLOUD_FREQUENCY_PENALTY` | `0.3` | No |  | LLM sampling setting. |
| `SLEUTH_CLOUD_MAX_TOKENS` | `600` | No | secret | LLM response token cap. |
| `SLEUTH_CLOUD_PRESENCE_PENALTY` | `0.2` | No |  | LLM sampling setting. |
| `SLEUTH_CLOUD_TEMPERATURE` | `0.6` | No |  | LLM temperature for free-form answers. |
| `SLEUTH_CLOUD_TEMPERATURE_TOOLS` | `0.0` | No |  | LLM temperature for tool calling (0 = deterministic). |
| `SLEUTH_CLOUD_TIMEOUT_S` | `20` | No |  | Advanced tuning (optional; defaults shown) |
| `SLEUTH_DOCS_DIR` | `<repo>/docs` | No |  | Folder of extra documents indexed by RAG. |
| `SLEUTH_EVAL_ENABLED` | `False` | No |  | LLM-as-judge: flags low-confidence answers with a "please verify" note |
| `SLEUTH_EVAL_MIN_SCORE` | `0.5` | No |  | Answers scored below this get a 'please verify' note. |
| `SLEUTH_LLM_CTX_LEN` | `"1024"` | No |  | Local LLM context window (tokens). |
| `SLEUTH_LLM_IDLE_UNLOAD_S` | `"600"` | No |  | Unload the model after this many idle seconds; load only when free RAM is at least model size x SLEUTH_LLM_RAM_HEADROOM. |
| `SLEUTH_LLM_MAX_TOKENS` | `"120"` | No | secret | Local LLM answer length cap. |
| `SLEUTH_LLM_MODEL_PATH` | `<repo>/models/sleuth.gguf` | No |  | Optional local LLM (llama.cpp, needs `pip install llama-cpp-python` and a GGUF model). Dormant when the model file is absent. |
| `SLEUTH_LLM_RAM_HEADROOM` | `"1.4"` | No |  | Load the local model only when free RAM >= model size x this factor. |
| `SLEUTH_LLM_THREADS` | `"1"` | No |  | CPU threads for local inference. |
| `SLEUTH_LLM_TIMEOUT_S` | `"12"` | No |  | Local inference timeout in seconds. |
| `SLEUTH_LLM_TOOLS_ENABLED` | `False` | No |  | (create/transition/assign/comment/sprint lifecycle) always stops for explicit user confirmation first; there is no delete tool at all. |
| `SLEUTH_LLM_TOOLS_MAX_ROUNDS` | `4` | No |  | Tool-call rounds per chat turn. |
| `SLEUTH_RAG_DIR` | `<repo>/.sleuth_rag` | No |  | Where the local vector index is stored. |
| `SLEUTH_RAG_ENABLED` | `False` | No |  | Retrieval-Augmented Generation over bugs, comments and SLEUTH_DOCS_DIR (needs chromadb and a Gemini embeddings key) |
| `SLEUTH_RAG_TOP_K` | `5` | No |  | Documents retrieved per RAG query. |
| `SLEUTH_RETRIEVAL_ENABLED` | `False` | No |  | Keyword grounding over real bug records (no vector DB) and a citation check that flags bug numbers an answer cites without evidence (cloud layer only) |
| `SLEUTH_VERIFY_ANSWERS` | `False` | No |  | Flag bug numbers an answer cites without evidence (cloud layer only). |

## Sign-in protection

| Variable | Default | Required | Secret | Purpose |
|---|---|---|---|---|
| `LOGIN_FAIL_LIMIT` | `10` | No |  | Per-account lockout: after LOGIN_FAIL_LIMIT failed sign-ins within LOGIN_FAIL_WINDOW_SECONDS, the account is locked for LOGIN_LOCKOUT_SECONDS (0 disables). Per process, like the per-IP login rate limit. |
| `LOGIN_FAIL_WINDOW_SECONDS` | `900` | No |  | Window for counting failed sign-ins (see LOGIN_FAIL_LIMIT). |
| `LOGIN_LOCKOUT_SECONDS` | `900` | No |  | How long an account stays locked after too many failures. |
| `PASSWORD_BREACH_CHECK_ENABLED` | `True` | No |  | Reject passwords found in public breach corpora (Have I Been Pwned k-anonymity range API: only a 5-character hash prefix leaves the server; an outage never blocks a password change). |

## Observability

| Variable | Default | Required | Secret | Purpose |
|---|---|---|---|---|
| `OTEL_DEPLOYMENT_ENVIRONMENT` | `""` | No |  | Filter values in SigNoz by deployment environment (defaults to APP_ENV): |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `""` | No |  | SIGNOZ OBSERVABILITY (OTLP/gRPC). Blank endpoint = telemetry off (console-only). Keep the real collector URL in .env / platform secrets — never in code. The http:// scheme here means cleartext gRPC (not REST). |
| `OTEL_EXPORTER_OTLP_HEADERS` | `""` | No | secret | Collector auth headers, extra resource attributes (comma-separated key=value): |
| `OTEL_EXPORTER_OTLP_INSECURE` | `""` | No |  | Override TLS detection for the OTLP endpoint (derived from the URL scheme by default). |
| `OTEL_INSTRUMENT_HTTPX` | `True` | No |  | Trace outbound HTTP calls (LLM, GitHub, webhooks). |
| `OTEL_INSTRUMENT_SQLALCHEMY` | `True` | No |  | Auto-instrumentation toggles: |
| `OTEL_LOGS_ENABLED` | `True` | No |  | Export logs when the OTLP endpoint is set. |
| `OTEL_LOG_EXCLUDE_LOGGERS` | `""` | No |  | Loggers kept out of the export (access logs arrive as traces instead). |
| `OTEL_LOG_MIN_LEVEL` | `""` | No |  | Log export: floor level, loggers to keep out of SigNoz (requests are traces), and redaction of sensitive attribute keys: |
| `OTEL_LOG_REDACT_SENSITIVE` | `True` | No |  | Redact attribute keys that look like passwords, tokens, secrets or cookies before export. |
| `OTEL_METRICS_ENABLED` | `True` | No |  | Export metrics when the OTLP endpoint is set. |
| `OTEL_METRICS_EXPORT_INTERVAL_MS` | `60000` | No |  | Metrics export cadence (ms): |
| `OTEL_RESOURCE_ATTRIBUTES` | `""` | No |  | Extra resource attributes, comma-separated key=value. |
| `OTEL_SERVICE_NAME` | `""` | No |  | service.name reported to the collector. |
| `OTEL_SERVICE_VERSION` | `""` | No |  | service.version; defaults to APP_VERSION. |
| `OTEL_TRACES_ENABLED` | `True` | No |  | Signals exported when the endpoint is set (each can be switched off): |
| `OTEL_TRACES_EXCLUDED_URLS` | `""` | No |  | URL regexes kept out of server spans (health checks, static assets): |
| `OTEL_TRACES_SAMPLER` | `""` | No |  | Sampling: parentbased_always_on \| always_on \| always_off \| traceidratio \| parentbased_traceidratio (ratio via OTEL_TRACES_SAMPLER_ARG, default 1.0): |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | No |  | Ratio for the *traceidratio samplers. |

## Reports

| Variable | Default | Required | Secret | Purpose |
|---|---|---|---|---|
| `MAX_REPORT_ROWS` | `50000` | No |  | Row cap for one Reports Excel export (larger requests get 413). |

## Set by the platform (do not configure)

`CONTAINER_APP_HOSTNAME`, `CONTAINER_APP_NAME` and `CONTAINER_APP_ENV_DNS_SUFFIX` are injected by
Azure Container Apps and only used to derive a default `APP_BASE_URL`. `BCRYPT_TEST_ROUNDS` is a
test-suite speed knob.
