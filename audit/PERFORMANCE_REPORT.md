# Performance report

## Test environment

| Item | Value |
|---|---|
| Host | Windows 11, 32 logical CPUs, NVMe SSD |
| App | one Uvicorn worker (as in Compose), Python 3.12.13, `APP_ENV=development` |
| Database | PostgreSQL 16.13 (portable cluster), `pg_stat_statements` + `auto_explain` |
| Dataset | 30,603 work items across 6 projects, 60,000 comments, 120,000 audit rows, 30,000 notifications, 80 users, 3 agile projects with boards, 12 sprints and 600 ranked stories (created through the API) |
| Resource limits | Windows Job Object hard caps (kernel-enforced CPU rate and per-process commit limit): app 0.30 CPU / 320 MB, PostgreSQL 0.20 CPU / 192 MB, i.e. the `docker-compose.yml` split of 0.5 CPU / 512 MB |
| Limiter validation | busy loop capped at 0.3 cores averaged 0.278 cores; a 600 MB allocation under a 320 MB cap raised `MemoryError` |
| Workload | `perf_load.py`: virtual users sharing one admin session, zero think time, weighted mix: list (25%), search (10%), item detail (15%), board (8%), backlog (6%), burndown (4%), stats (6%), audit (4%), notifications (4%), create/update/comment (writers only, ~1 in 3 users), session check |

Zero think time makes each virtual user far heavier than a person: 20 such users
approximate a few hundred people clicking normally.

## Results

| Run | Limits | Pool | Users | Duration | Requests | Throughput | Errors | p50 | p95 | Peak app memory |
|---|---|---|---|---|---|---|---|---|---|---|
| Baseline | none | 5+5 | 20 | 60 s | 5,819 | 96.7 req/s | 0 (41 x 429 comment throttle) | 143-355 ms | 318-534 ms | 140 MB |
| App capped | app 0.3 CPU / 320 MB | 5+5 | 20 | 60 s | 1,683 | 28.0 req/s | 0 | 0.56-1.2 s | 1.2-1.8 s | 137 MB |
| Full stack capped (after index + harness fix) | 0.5 CPU / 512 MB | 5+5 | 20 | 60 s | 2,365 | 39.4 req/s | 0 | 0.52-0.60 s | 0.61-1.16 s | 111 MB (DB job 93 MB) |
| Spike, before fix | 0.5 CPU / 512 MB | 5+5 | 100 | 30 s | 103 | 1.7 req/s | **98-100 timeouts** | 30-60 s | 60 s | 123 MB |
| Spike, after fix | 0.5 CPU / 512 MB | 5+5 | 100 | 30 s | 1,255 | 39.3 req/s | 0 | ~2.4 s | ~3.0 s | 120 MB |
| Compose pool | 0.5 CPU / 512 MB | 2+2 | 20 | 60 s | 2,270 | 37.5 req/s | 0 (15 x 429) | - | - | 105 MB |
| Compose pool spike | 0.5 CPU / 512 MB | 2+2 | 100 | 30 s | 1,160 | 35.6 req/s | 0 | - | - | 118 MB |
| Soak | 0.5 CPU / 512 MB | 5+5 | 20 | 600 s | 23,934 | 39.9 req/s | 0 (117 x 429) | 0.53-0.60 s | 0.62-1.12 s | 108 MB, identical at 7 and 10 min |

Per-scenario figures are in the scratchpad `perf/*-load.json` files; the
PostgreSQL-side top queries are in `evidence/explain-before.txt`.

Start-up on the existing 30k-item database: 3.9-4.7 s under the 0.3-CPU cap.
The 3.1 -> 4.0 migration boot now takes 2.8 s; the 22 s measured in the first
audit was defect C01 (reflection waiting out `lock_timeout` twice, and silently
skipping columns), fixed in the closure pass.

## Docker Compose run (real cgroup limits)

Closure pass, Docker Desktop (Engine 29.8.1, WSL2 VM). The stack was started
with `deploy.sh` from a clean copy, with the Compose limits unchanged
(app 0.30 CPU / 320 MB, PostgreSQL 0.20 CPU / 192 MB, pool 2+2) and the same
30k-item dataset seeded into the Compose database. CPU and memory were sampled
with `docker stats` every 2 s. "HDD" runs added a blkio throttle on the device
backing `/var/lib/docker` (150 IOPS and 100 MB/s read and write), verified before
the runs inside the database container (1,500 direct 4 KB writes took 9.9 s:
151 IOPS); "cold" dropped the page
cache and restarted PostgreSQL first.

| Run | Users | Duration | Requests | Throughput | Errors | Worst p95 | App CPU / mem peak | DB CPU / mem peak |
|---|---|---|---|---|---|---|---|---|
| Load | 20 | 60 s | 2,052 | 33.8 req/s | 0 (13 x 429) | 0.96 s | 32% / 145 MB | 20% / 116 MB |
| Spike | 100 | 32 s | 1,091 | 33.7 req/s | 0 | 3.5 s | 32% / 146 MB | 20% / 116 MB |
| HDD load | 20 | 60 s | 2,098 | 34.8 req/s | 0 | 0.92 s | 30% / 146 MB | 20% / 51 MB |
| HDD spike | 100 | 33 s | 1,107 | 33.9 req/s | 0 | 3.3 s | 30% / 147 MB | 20% / 51 MB |
| HDD cold cache | 20 | 30 s | 989 | 32.5 req/s | 0 | 1.4 s | 31% / 140 MB | 19% / 53 MB |
| HDD soak | 20 | 600 s | 20,797 | 34.6 req/s | 0 (67 x 429) | 0.90 s | 33% / 148 MB | 21% / 58 MB |

CPU percentages are of one core, so the app sat at its 0.30 cap and PostgreSQL
at its 0.20 cap: the stack is CPU-bound and degrades by queueing, never by
erroring. Memory stayed under half of each limit and was flat through the soak
(app 147-148 MB in the last 5 minutes). Throttled storage made no measurable
difference because the hot data fits PostgreSQL's cache; only the cold-cache
run paid for it (p95 1.4 s for the first 30 s).

## Sprints on the large dataset (production-mode Compose stack)

The redesigned Sprints endpoints, measured on the Compose stack's 31,000-issue
PostgreSQL database (disk throttled to hard-disk limits by
`hdd-override.yml`), on its largest project (5,383 issues). Five requests
each, after the first boot had re-spaced every backlog list.

| Endpoint | Median | Max |
|---|---|---|
| Backlog view (`/boards/{id}/planning`) | 204 ms | 604 ms |
| Active sprint board (`/boards/{id}/view`) | 185 ms | 187 ms |
| Epics tree (`/projects/{id}/hierarchy`) | 199 ms | 404 ms |
| Sprint list | 7 ms | 70 ms |
| Velocity report | 5 ms | 91 ms |
| Cumulative flow, 30 days | 296 ms | 502 ms |
| Control chart, 30 days | 279 ms | 500 ms |
| Backlog drag (`/boards/{id}/move`) | 96 ms | 104 ms |

The first boot of this database re-ranked 31,011 issues (one-time upgrade);
the next boot did no upgrade work.

## Bottlenecks found and fixed

1. **Pool deadlock (critical for availability).** With more concurrent
   requests than connections, requests held connections between worker-thread
   hops while every worker waited for a connection. Symptom: app CPU near
   zero, all connections `idle in transaction`, 30 s timeouts. Fixed with
   admission control in `get_db` (see AUDIT_REPORT F01). With Compose's 2+2
   pool the old code would stall at far lower concurrency (reasoned from the
   mechanism, not re-measured on the old code).
2. **Single-project list query.** Page 20 of one project walked the global
   `updated_at` index (5,259 rows, 9.5 ms). New composite index
   `idx_bugs_project_updated_id`: 0.23 ms. Multi-project filters were
   unaffected either way (3.8 ms).

## Query plans (30k items, after the index)

All hot paths are index-driven: audit page 0.13 ms, admin list page 1
0.05 ms, search count 3.1 ms (bitmap on status), notifications 0.5 ms, unread
count 0.11 ms (index-only), comments/activity per item < 0.1 ms, sprint members
0.28 ms, backlog ranking 2.9 ms.

## Resource-constrained verdict

| Target | Outcome | Status |
|---|---|---|
| 0.5 CPU | Enforced; the app is CPU-bound at the cap and degrades by queueing, never by erroring | PASS |
| 512 MB | Enforced; app peak 105-123 MB, PostgreSQL job peak 93 MB | PASS |
| HDD-like storage | blkio throttle at 150 IOPS: no throughput change warm, p95 1.4 s cold | PASS |
| Container runtime (cgroups) | Compose limits enforced by Docker; results match the Job Object runs | PASS |

## Not tested

- More than one Uvicorn worker or replica (the rate limiters and digest
  scheduler are per process; see KNOWN_LIMITATIONS).
- Frontend load metrics (time to interactive). Bundle sizes from the Vite
  build: largest chunks `index` 163 kB (51 kB gzip), `styles` 137 kB (44 kB
  gzip), `SprintsView` 120 kB (35 kB gzip), lazily loaded per view.
