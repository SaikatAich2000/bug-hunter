<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Operations

> [← Back to the README](../README.md)

Everything here is for the Docker Compose deployment (`docker-compose.yml`, `deploy.sh`,
`down.sh`). Settings are environment variables; every one is listed in
[`.env.example`](../.env.example) and read in `app/config.py`.

## Deploy and upgrade

```bash
cp .env.example .env     # or: python scripts/gen_local_env_secrets.py (fills the secrets)
./deploy.sh              # http://localhost:8765
```

`deploy.sh` refuses to run while `POSTGRES_PASSWORD`, `SESSION_SECRET` or
`BOOTSTRAP_ADMIN_PASSWORD` is empty or a placeholder. If the database container is
already running it takes a backup first (`SKIP_BACKUP=1` skips that), then builds, starts
PostgreSQL, waits for it to be healthy, then starts the app. The `bugtracker_pgdata`
volume is never touched.

To upgrade: set the new `APP_VERSION` in `.env`, run `./deploy.sh`. There is no migration
step: `init_db()` adds missing tables, columns, indexes and `CHECK` constraints on boot and
never drops data. On PostgreSQL an advisory lock serializes concurrent boots. New CHECK
constraints are added `NOT VALID`, so existing rows are not rescanned or rejected.

## Production start-up checks

With `APP_ENV=production` (or `prod`/`staging`) the app logs every unmet requirement and
refuses to start:

- `SESSION_SECRET` at least 32 characters and not a placeholder;
- a bootstrap admin password that is not the placeholder and meets the password policy;
- `AUTO_LOGIN_ENABLED` off;
- a well-formed `GIT_CREDENTIAL_ENCRYPTION_KEY` if one is set;
- `COOKIE_SECURE=true`, an `https://` `APP_BASE_URL`, and `EMAIL_BACKEND` other than `console`.

`COOKIE_SECURE=true` alone also counts as production for the first four checks.

## Backup and restore

```bash
./scripts/backup.sh                          # backups/bughunter-<UTC>.dump
BACKUP_KEEP=30 BACKUP_DIR=/mnt/backups ./scripts/backup.sh
./scripts/restore.sh backups/bughunter-<UTC>.dump     # --force skips the prompt
```

- `backup.sh` runs `pg_dump -Fc` inside `bugtracker_db`, verifies the dump with
  `pg_restore --list`, writes it with mode 600 and keeps the newest `BACKUP_KEEP`
  (default 14). It exits non-zero when the database container is not running.
- `restore.sh` validates the archive, asks you to type `restore`, takes a safety dump of the
  current data, stops the app, runs `pg_restore --clean --if-exists --single-transaction`,
  and starts the app again. The safety dump's name is printed, so a restore can be undone.
- Attachments live in the database, so one dump covers everything. Copy dumps off the
  host; a backup on the same disk does not survive losing it. Restore into a scratch stack
  now and then to prove the dumps work.
- Sessions stay valid across a restore only if `SESSION_SECRET` is unchanged.

### Drill

The following sequence was run against a Compose stack and each step verified: take a backup
(`./scripts/backup.sh`), delete data, restore it with `./scripts/restore.sh` and confirm the rows
came back, then set the previous `APP_VERSION`, run `./deploy.sh` and confirm the data was still
there (the schema is additive, so an older release runs against it). Repeat it on your own host
before relying on the dumps.

## Data retention

The app runs `app/jobs/retention.py` daily (first run a few minutes after start) unless
`RETENTION_JOB_ENABLED=false`. It deletes, in small batches:

| What | Rule |
|---|---|
| Notifications | older than `NOTIFICATION_RETENTION_DAYS` (default 180; 0 keeps) |
| Sleuth conversations | idle longer than `CHAT_RETENTION_DAYS` (default 180; 0 keeps) |
| Sessions, used or expired reset tokens, expired idempotency keys | always |

Audit rows and work items are never pruned. To use an external scheduler instead, disable
the built-in run and call `python -m app.jobs.retention`. It is safe on several replicas.

## Health, logs, tracing

- `GET /api/health` returns `{"status","database","version","asset_version"}`: 200 when the
  database answers, 503 (`"degraded"`) when it does not. The image's `HEALTHCHECK` uses it.
  Startup DB work is bounded by `DB_STARTUP_TIMEOUT_SECONDS`, so a stuck database yields a
  degraded app, not a hung container.
- Every response has an `X-Request-ID` (a safe client-supplied value of 8 to 64 characters
  is kept). It is on every log line the request causes. Ask users for it when reporting
  an error.
- `LOG_FORMAT=json` writes one JSON object per line. `LOG_LEVEL=DEBUG` adds SMTP detail.
- Logs: `docker compose logs -f app`.

### OpenTelemetry

Off until `OTEL_EXPORTER_OTLP_ENDPOINT` is set (OTLP/gRPC, e.g. `http://otel-collector:4317`;
`http://` means cleartext gRPC). Then traces (FastAPI, SQLAlchemy, httpx), metrics and logs
are exported. Variables, all read in `app/telemetry.py`:

| Variable | Default |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | blank = off |
| `OTEL_EXPORTER_OTLP_INSECURE` | not `https://` |
| `OTEL_EXPORTER_OTLP_HEADERS` | none (`k=v,k=v`) |
| `OTEL_SERVICE_NAME` / `OTEL_SERVICE_VERSION` | `bug-hunter` / `APP_VERSION` |
| `OTEL_DEPLOYMENT_ENVIRONMENT` | `APP_ENV` |
| `OTEL_RESOURCE_ATTRIBUTES` | none |
| `OTEL_TRACES_ENABLED` / `OTEL_METRICS_ENABLED` / `OTEL_LOGS_ENABLED` | `true` |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` / `1.0` |
| `OTEL_TRACES_EXCLUDED_URLS` | health checks and `/static` |
| `OTEL_METRICS_EXPORT_INTERVAL_MS` | `60000` |
| `OTEL_LOG_MIN_LEVEL` / `OTEL_LOG_EXCLUDE_LOGGERS` | `INFO` / `uvicorn.access,watchfiles` |
| `OTEL_LOG_REDACT_SENSITIVE` | `true` |
| `OTEL_INSTRUMENT_SQLALCHEMY` / `OTEL_INSTRUMENT_HTTPX` | `true` |

No traces? Check the start-up log for `OTLP export enabled`, that the signal is not
disabled, that the sampler is not `always_off`, and that the collector filter includes your
`OTEL_DEPLOYMENT_ENVIRONMENT`.

## Scaling limits

- The stack runs one Uvicorn worker. Login lockout, the per-IP, per-user and chat rate
  limiters, and the in-app digest scheduler keep their state in process memory. With
  several workers or replicas each has its own counters, so put a limit in the proxy and
  run the digest from one place.
- Postgres pool: `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` (Compose sets a small pool).
- Behind a reverse proxy set `TRUST_PROXY_FORWARDED_FOR=true` and `TRUST_PROXY_HOP_COUNT`
  to the number of trusted hops; otherwise the app sees only the proxy's address. Without
  it, per-IP limits and lockout apply to the proxy as a whole.

## Sleuth and the docs folder

When `SLEUTH_RAG_ENABLED=1`, the index includes every `.md` and `.txt` file under
`SLEUTH_DOCS_DIR` (default `docs/`), so these pages become assistant knowledge. Point it
elsewhere if that is not wanted.

## Configuration reference

Every deployment- or environment-specific value is a `Settings` attribute in `app/config.py`, read from the
environment or `.env`; `.env.example` lists each with its default (the default equals the former built-in value).
Tunable groups: rate limits (`RATE_*`), login lockout (`LOGIN_*`), paging and size (`PAGE_SIZE_*`,
`MAX_ATTACHMENT_BYTES`, `MAX_*_BODY_BYTES`), timeouts and retries (`DB_*`, `SMTP_TIMEOUT`, `GITHUB_*`,
`HIBP_TIMEOUT_SECONDS`, `SLEUTH_CLOUD_TIMEOUT_S`), third-party endpoints (`HIBP_API_URL`, `GROQ_API_URL`,
`OPENROUTER_API_URL`, `GEMINI_API_BASE_URL`, `GITHUB_API_URL`, `FCM_CONNECT_SRC`), URLs and CORS (`APP_BASE_URL`,
`CORS_ORIGINS`), identity (`APP_NAME`, `EMAIL_FROM`, `BOOTSTRAP_ADMIN_*`), retention, and listener (`UVICORN_*`).
Also configurable: bulk-import limits (`IMPORT_MAX_*`), Sleuth memory, confirmation, export-cache, bulk-cap, upload and breaker values (`SLEUTH_*`), retention schedule (`RETENTION_*`), `IDEMPOTENCY_EXPIRY_HOURS`, `DEFAULT_SPRINT_DAYS`, `BACKLOG_DEFAULT_LIMIT`, `DB_POOL_RECYCLE_SECONDS`. Browser poll intervals (`DATA_POLL_SECONDS`, `SESSION_POLL_SECONDS`, `NOTIFICATION_POLL_SECONDS`, minimum 5) are served in `/api/meta`; the frontend falls back to 30/30/15 s until it answers.
Numeric values below their minimum are clamped; unparsable values fall back to the default.

## Local run

`scripts/run_local.ps1` starts the app on port 8000 with the SQLite file `bug_hunter.db` in the repo (gitignored).
`.env` comes from `scripts/gen_local_env_secrets.py` (secrets generated, never printed). Mail goes to a local Mailpit:

    docker run -d --name bh-mailpit -p 1025:1025 -p 8025:8025 axllent/mailpit

Set `EMAIL_BACKEND=smtp`, `SMTP_HOST=127.0.0.1`, `SMTP_PORT=1025`, `SMTP_USE_TLS=false`; read mail at
http://127.0.0.1:8025. With the server running, `python scripts/seed_local.py` creates two demo projects,
manager and user accounts and sample items through the API (idempotent; values come from `.env`).
The bootstrap admin is only created in an empty database, so delete `bug_hunter.db` to change it.


## Incidents, recovery targets and alerts

### Recovery targets

| Target | Value | Basis |
|---|---|---|
| Data loss window (RPO) | up to the backup interval: 24 hours with a nightly `scripts/backup.sh`; minutes with point-in-time recovery from a managed PostgreSQL plan | backups are only as fresh as the schedule you run |
| Time to restore (RTO) | minutes for a small database | the restore drill (backup, delete data, `scripts/restore.sh`) returned every table byte-identical; the app restarts on its own afterwards |
| Planned deploy | about 20 s of cold start on a 0.1 CPU instance, none of it serving traffic | measured in [performance.md](performance.md) |

Take a backup before every upgrade, copy it off the host, and rehearse one restore a month: a backup that has never been restored is a hope, not a backup.

### When the service is down or slow

1. **Is it up?** `GET /api/health` answers `status: ok` when the database is reachable. `degraded` means the app is running but the database is not.
2. **Database lost or exhausted:** the API answers `503` with `Retry-After`, never a bare `500`, and recovers by itself when the database returns (tested with a 30-second outage). Check the database service first; then `DB_POOL_SIZE` and the connection limit of the database plan.
3. **Everything is slow:** the instance is probably CPU-bound. Look at CPU and request rate; on a 0.1 CPU instance a sign-in (bcrypt) stalls other requests for about 2 s, see `BCRYPT_ROUNDS` in [performance.md](performance.md).
4. **Many 429 responses:** per-address limits are working. A shared office address or a load test can trip them; raise `RATE_LIMIT_SCALE` or `API_RATE_MAX` for a known address range only if the traffic is legitimate.
5. **Bad release:** redeploy the previous image tag. The schema changes are additive, so the older version keeps working against a newer schema.
6. **Suspected account takeover:** end the user's sessions (Sessions view, or change their password, which bumps the session version), check the audit trail for sign-in, reset and e-mail-change events, and ask the user to enable two-factor authentication.
7. **Suspected data exposure:** rotate `SESSION_SECRET` (ends every session) and the encryption key, restore from a clean backup if rows were altered, and follow the disclosure steps in [SECURITY.md](../SECURITY.md).

After an incident write down what happened, how it was noticed, and what changed; add a test when the cause was a bug.

### Alerts worth setting

With an OpenTelemetry backend (see [observability.md](observability.md)) or the Prometheus endpoint, alert on:

| Alert | Rule of thumb |
|---|---|
| Uptime | `/api/health` fails 2 checks in a row (use an external checker; it also keeps a free-tier instance awake) |
| Server errors | 5xx responses above 2% of requests for 5 minutes |
| Database unavailable | any 503 for more than 1 minute |
| Latency | p95 above 1 s for 10 minutes |
| Memory | resident memory above 80% of the limit (400 MB of 512 MB) |
| Sign-in abuse | a sudden rise in failed sign-ins or lockouts (audit trail, `bh.events` counter) |
| Backups | no new backup file in 26 hours |

## Troubleshooting

See [Troubleshooting](troubleshooting.md).
