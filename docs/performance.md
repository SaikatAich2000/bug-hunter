<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Performance

> [← Back to the README](../README.md)

Measured results are in [Results](#results); they come from `scripts/loadtest.py` on one test machine
and will differ on other hardware.

## What is limited by design

- Compose caps the stack at 0.5 vCPU / 512 MB (app 0.30 / 320 MB, database 0.20 / 192 MB) and
  runs one Uvicorn worker.
- Postgres pool: `DB_POOL_SIZE` + `DB_MAX_OVERFLOW`; statements time out after
  `DB_STATEMENT_TIMEOUT_MS` (30 s) and lock waits after `DB_LOCK_TIMEOUT_MS` (10 s).
- Bodies: `MAX_REQUEST_BODY_BYTES` (60 MiB, uploads) and `MAX_JSON_BODY_BYTES` (2 MiB).
  Attachments are 50 MB each; Excel exports stop at `MAX_REPORT_ROWS` (50 000, then 413).
- Login is 8 requests/min/IP and writes are rate-limited per user (comments 30/min,
  uploads 20/min). The load script therefore signs in once and shares the session.

## Measuring

Use a throwaway instance with a user that can create and delete work items (an admin), and
a project id to create items in. The script creates items titled `loadtest-...` and deletes
them at the end of each level.

```bash
pip install psutil                      # optional, for server RSS
# Find the server pid: Docker: docker top bugtracker_app; local: the uvicorn process.
python scripts/loadtest.py --base-url http://localhost:8765 \
    --email admin@example.com --password '...' --project-id 1 \
    --levels 10,25,50,100,200 --duration 30 --pid <server pid>
```

Each virtual user loops: four reads (`/api/auth/me`, `/api/projects`, `/api/bugs`,
`/api/stats`), then one write (`POST /api/bugs`). The script prints, per level and for
reads and writes: requests per second, p50, p95, p99 (ms), error count (HTTP 4xx/5xx or
transport failures), and the peak server RSS. A 429 counts as an error, so a level that
trips a limiter shows up as errors rather than as a good number.

For the container's CPU and memory as Docker sees them (no pid needed), use the read-only
script: `python scripts/load_test.py --base-url ... --users 15 --duration 90 --docker-container bugtracker_app`.

## Results

Measured with `scripts/loadtest.py` (first run, before the SQL-per-request work). Method: closed loop,
zero think time, four reads to one write per virtual user, login once and share the session. App
container limited to 1 vCPU, DB pool 5+5, PostgreSQL 16. Command to reproduce: see Measuring above.

| Users | Total req/s | Read p95 ms | Write p95 ms |
|---|---|---|---|
| 10 | 111 | 120 | 168 |
| 25 | 112 | 323 | 400 |
| 50 | 111 | 660 | 759 |
| 100 | 111 | 1410 | 1540 |
| 200 | 95 | 5002 | 6524 |

- Errors: 0 5xx.
- Unloaded latency: 3-11 ms.
- Peak RSS: 143 MiB (target 256).
- 30-minute soak at 50 users: RSS 143 -> 144 -> 143 MiB (flat), read p95 576 ms, write p95 623 ms, 0 errors.
- The first runs were not isolated from a concurrent Gradle build on the test machine, so numbers may be pessimistic.

SQL-per-request work (pinned by `tests/test_sql_budget.py`, which asserts statements per endpoint): Statements per request, before -> after: `/api/stats` 10 -> 6.

A second local run (uvicorn, single worker) measured about 133 req/s total for both repos at 100 users.
The load generator saturates there (the health endpoint alone reaches ~140 req/s), so that run is
client-bound and is not a capacity figure.

### Status against targets

- Throughput of at least 2x the old ~42 req/s baseline: met (111 req/s).
- Read p95 under 500 ms and write p95 under 800 ms: met at 10 users (and 25 for BH); not met from 50 users up in this zero-think-time closed loop.
- Zero 5xx: met. Peak RSS under 256 MiB: met. Flat memory over the soak: met.

## Reading the result

- Throughput is flat at roughly 111 req/s from 10 users upward: one CPU is saturated, and extra users only queue.
  By Little's law, 111 req/s with 100 users means each request waits about 0.9 s on average. The latency
  targets are therefore only meaningful with think time (realistic users) or more CPUs.
- Do not quote latency at 200 users as capacity; it measures queueing, not service time.
- Compare RSS with the 320 MB app limit in Compose. A container killed for memory restarts
  and the load script shows transport errors.
- Errors above zero at a level means that level is past what the deployment sustains (or a
  limiter fired).
- SQLite results do not transfer; these figures are PostgreSQL only.


## Small instances: 0.1 CPU, 512 MB, slow disk

The app is meant to stay responsive on a free-tier web service (a tenth of a core, 512 MB) with the database
on a separate service. Measured with the container capped at exactly 0.1 CPU and 512 MB:

| What | Result |
|---|---|
| Cold start to a healthy `/api/health` | about 20 s (the imports of FastAPI, SQLAlchemy and Pydantic dominate) |
| Idle memory | about 120 MB; 30 minutes under load stayed under 140 MB |
| Everyday calls (`/api/auth/me`, projects, small lists, create a work item) | 4-110 ms |
| First load of the large JavaScript and CSS bundles (11 files at once) | 400 ms cold, 110 ms once cached |
| Sign in | about 1.9 s at bcrypt cost 12 |
| Capacity | roughly 10 requests per second at 0.1 CPU (27-38 at 0.3 CPU); beyond that latency grows and nothing fails |

What keeps it light:

- **Static files** are compressed once per process and served from memory (`app/static_files.py`), not recompressed on every request.
- **Background polling** slows to a quarter after two idle minutes and stops after ten (`frontend/src/lib/poller.js`); hidden tabs never poll. A visible, active tab costs about 0.15 requests per second.
- **Per-request cost** is about 2-4 ms of CPU: one session lookup, a handful of statements, no N+1 (`tests/test_sql_budget.py` pins the statement counts).
- **Database**: every foreign key has an index, so deletes stay cheap as tables grow. SQLite (self-hosted) runs in WAL mode with `synchronous=NORMAL`, which suits a spinning disk.
- **Telemetry** costs nothing until `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

Settings worth changing on a small instance:

- `BCRYPT_ROUNDS=10` (the minimum the app allows and OWASP's floor) makes sign-in about four times faster. Every sign-in stalls other requests for its duration, because the whole container is throttled together. Existing password hashes keep working.
- `DB_POOL_SIZE` 3-5 and `DB_MAX_OVERFLOW` 2-5; a larger pool did not make anything faster in testing and holds more database memory.
- Keep one Uvicorn worker (the default). Several workers share the same tenth of a core and multiply memory.
- A free-tier service sleeps when idle and the next request waits for the cold start; an external uptime check keeps it warm.
