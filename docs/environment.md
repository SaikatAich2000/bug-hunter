<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Key settings, email, push and telemetry

The settings that matter most.

> [← Back to the README](../README.md)

## Configuration

All settings come from environment variables. Copy [`.env.example`](../.env.example)
to `.env`. Every variable is explained inline there and read in
[`app/config.py`](../app/config.py). Never commit `.env`. Secret values belong in
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

### Observability (OpenTelemetry / SigNoz)

Set `OTEL_EXPORTER_OTLP_ENDPOINT` and the app streams three signals to the
collector over OTLP/gRPC. Leave it blank and everything stays on the console.

| Signal | Details |
| --- | --- |
| **Traces** | One server span per HTTP request from FastAPI, DB spans from SQLAlchemy, and outbound spans from `httpx`. Health checks and `/static` are excluded by default (`OTEL_TRACES_EXCLUDED_URLS`). |
| **Metrics** | HTTP server and DB client metrics, exported every `OTEL_METRICS_EXPORT_INTERVAL_MS` (default 60 s). |
| **Logs** | The same `bug_hunter.*` records as the console, with `trace_id`/`span_id` so a log pivots to its request. Audit-trail entries arrive as `audit …` records. Sensitive attribute keys are redacted before export (`OTEL_LOG_REDACT_SENSITIVE`). |

The service name defaults to `bug-hunter` (`OTEL_SERVICE_NAME`), and the
deployment environment defaults to `APP_ENV` (`OTEL_DEPLOYMENT_ENVIRONMENT`).
Per-signal switches (`OTEL_TRACES_ENABLED`, `OTEL_METRICS_ENABLED`,
`OTEL_LOGS_ENABLED`), the sampler (`OTEL_TRACES_SAMPLER`), collector headers and
TLS options are all documented in `.env.example`.

**No traces?** Check, in order: the startup log says `OTLP export enabled -> …`,
`OTEL_TRACES_ENABLED` isn't `false`, the sampler isn't `always_off`, and your
collector's environment filter includes `OTEL_DEPLOYMENT_ENVIRONMENT`.

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
