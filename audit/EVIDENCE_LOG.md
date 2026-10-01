# Evidence log

Running log of commands and outcomes for the production-readiness pass on
branch `feature/xops-parity` (started 2026-09-30). Newest entries at the bottom.
Raw outputs live outside the repository in the session scratchpad (`evidence/`);
only concise results are recorded here.

Environment: Windows 11, 32 logical CPUs, Python 3.12.13 (`.venv`, uv-managed),
Node 20.18.1 (portable), no Docker, no SonarQube server, no PostgreSQL service.

| # | Area | Command | Result | Status |
|---|---|---|---|---|
| 1 | Backend suite (pre-pass) | `.venv/Scripts/python -m pytest -m "not ui" -q -p no:cacheprovider --no-cov -o addopts=""` | 2390 passed, 2 skipped, 14 deselected (947 s) | PASS |
| 2 | Python lint | `ruff check app tests scripts` | All checks passed | PASS |
| 3 | Python SAST | `bandit -c pyproject.toml -r app -q` | 0 findings | PASS |
| 4 | Python compile | `python -m compileall -q app tests scripts` | exit 0 | PASS |
| 5 | Python deps (runtime) | `pip-audit -r requirements-lock.txt --strict --disable-pip --no-deps` | No known vulnerabilities | PASS |
| 6 | Python deps (dev) | `pip-audit -r requirements-dev-lock.txt --strict --disable-pip --no-deps` | No known vulnerabilities | PASS |
| 7 | pip-audit resolver mode on Windows | `pip-audit -r requirements-lock.txt --strict` | Fails: lock pins `uvloop` (Linux-only) | NOT APPLICABLE on Windows (CI runs Linux) |
| 8 | npm prod deps (baseline) | `npm audit --omit=dev --audit-level=high` | 0 vulnerabilities | PASS |
| 9 | npm all deps (baseline) | `npm audit` | 6 vulns: happy-dom (3 critical, unused), esbuild/vite/vitest chain (moderate/high, dev server only) | FAIL -> fixed (#10) |
| 10 | npm all deps (after) | removed unused `happy-dom`; vite 5.4 -> 6.4.3; vitest/@vitest/coverage-v8 1.6 -> 4.1.11; `npm audit` | found 0 vulnerabilities | PASS |
| 11 | Frontend tests after upgrade | `npx vitest run` | exposed 2 corrupted test files (tests nested inside tests, never executed under Vitest 1) | FAIL -> fixed (#12) |
| 12 | Frontend tests (repaired) | `npx vitest run` | 27 files, 263 passed (was 248; 15 tests had silently never run) | PASS |
| 13 | Push retry defect | test "stops cleanly when the FCM SDK is blocked" (newly executed) | retried ~6.5 min on a permanent condition; fixed in `frontend/src/lib/push.js` | PASS |
| 14 | ESLint | `npm run lint` | clean | PASS |
| 15 | Frontend coverage | `npm run test:coverage` | 17.27% statements, 17.58% lines (6542 statements in scope) | PARTIAL |
| 16 | Frontend build | `npm run build` (Vite 6.4.3) | built in 1.55 s | PASS |
| 17 | Semgrep SAST | `semgrep scan --config p/python p/javascript p/react p/secrets p/dockerfile p/github-actions` (274 rules, 202 files) | 30 findings, 0 errors; triaged in `audit/SECURITY_REPORT.md` | PASS (after triage) |
| 18 | Open redirect | Semgrep `js-open-redirect` on `LoginPage.jsx`; manual proof `next=/%09/evil.example` bypassed the `^/(?![/\])` guard | fixed with URL-origin check (`frontend/src/lib/safeRedirect.js`), unit + browser tests | PASS |
| 19 | GitHub Actions pinning | 19 `uses:` on mutable tags | all 19 pinned to commit SHAs; `actionlint` 1.7.12 exit 0 | PASS |
| 20 | CI triggers | workflow only ran on `dev` (no such branch in this repo) | retargeted to `main`; SonarQube + image push made opt-in via repo variables | PASS |
| 21 | Dockerfile lint | `hadolint` 2.15.1 | 4 findings -> 2 fixed (merged RUN, numeric UID 1000), 2 justified ignores; exit 0 | PASS |
| 22 | Shell lint | `shellcheck` 0.11.0 on deploy.sh, down.sh, scripts/*.sh | 3 x SC2015 (info) reviewed: benign | PASS |
| 23 | Secret scan | `detect-secrets scan` on 357 tracked+new files; real-format key regex over tree and git history | only fixtures/placeholders; history hits are redaction-test samples (one sample's authenticity left to the owner, see HUMAN_ACTIONS) | PASS |
| 24 | Dead code | `vulture app --min-confidence 60` + reference count | 5 `auth.can_*` helpers + `get_current_user_optional` referenced only by tests | fixed in cleanup |
| 25 | Browser install | `playwright install` (Node downloader times out on this network) | browsers fetched with curl from the same CDN URLs; Chromium 153, Firefox 155, WebKit 26.6 launch | PASS |
| 26 | Existing UI suites | `pytest -m ui` | 15 passed, 13 skipped (`test_playwright_sprints.py` needs a hand-started server; mostly assertion-free) | replaced (#27) |
| 27 | New E2E suite | `PW_BROWSERS=chromium,firefox,webkit pytest tests/test_e2e_browser.py tests/test_ui_smoke.py tests/test_playwright_project_state.py -m ui` | 105 passed (121 s) | PASS |
| 28 | Notification deep links | `/#bug=N` / `/#event=N` from email, push, desktop | were ignored by the SPA (pre-existing since v3); now opened after boot and after login | PASS |
| 29 | Accessibility | axe-core 4.13.0, WCAG 2.1 A/AA, serious+critical, 9 pages x dark/light | baseline 7 of 9 dark pages failing (contrast, unnamed selects/editors, black event titles); now 18/18 pass | PASS |
| 30 | API fuzzing (baseline) | `st run <openapi> --checks all --max-examples 25 --workers 4 --seed 1234` (Schemathesis 4.28, 157 ops, 5,534 cases) | 14 HTTP 500s (all `OverflowError`: ids past 64 bits), 66 response-schema violations (offset-less timestamps), doc gaps | FAIL -> fixed |
| 31 | Out-of-range ids | `OverflowError`/`DataError` -> 422 (FastAPI validation shape) in `app/main.py`; `tests/test_api_robustness.py` | 9 regression tests pass | PASS |
| 32 | Timestamps | `UTCDateTime` type in `app/models.py` (60 columns): SQLite results tagged UTC so browsers stop shifting times | regression test passes; DDL unchanged | PASS |
| 33 | Sprint name conflicts | duplicate name on create/rename raised IntegrityError -> 500 (service flushes inside the route's try-less call) | now 409; parametrized test over sprint/release/component/collection | PASS |
| 34 | Daily report date | `report_date=0000-00-00` leaked a Python `strptime` message | clean 422 | PASS |
| 35 | File endpoints OpenAPI | 6 download endpoints documented `application/json` | now document xlsx/csv/octet-stream | PASS |
| 36 | API fuzzing (after) | same command, `--max-examples 60`, 153 ops, 11,717 cases | 1 server error (sprint IntegrityError, fixed in #33); remaining findings are schema-precision gaps (see TESTING_REPORT) | PASS (500s) |
| 37 | PostgreSQL 16.13 (portable, scratch cluster) | `initdb` + `pg_ctl` on 127.0.0.1:55433 | available for DB tests | PASS |
| 38 | 3.1 -> 4.0 upgrade on populated PG | `pg_upgrade_test.py`: v3.1 (git HEAD) populates 3 projects, 2 users, 36 items, comments, 4 attachments, links, event; v4 boots twice | 22/22 checks: data, attachment bytes (sha256), logins, id counter, display ids, agile on legacy data, idempotent second boot, no log errors | PASS |
| 39 | Rich-text descriptions after upgrade | same test | API returns 3.1 HTML descriptions as plain text (xOPS design); stored HTML untouched until next save; description editor showed a formatting toolbar whose output was discarded | PARTIAL: toolbar hidden for descriptions; behaviour documented |
| 40 | Concurrent boot (2 replicas, empty PG) | `pg_race_test.py` x3 | before: second replica ran bootstrap + board backfill before tables existed / outside the lock (ERROR logs, duplicate-seed risk); after: startup writes run under the advisory lock, 3/3 clean | PASS |
| 41 | clean_db.py on PG | `scripts/clean_db.py --yes --database-url <copy of upgraded DB>` | first-launch baseline kept; `bugs_id_seq` restarts at 1 | PASS |
| 42 | Query plans (30,603 items, 60k comments, 120k audit rows, 30k notifications) | `EXPLAIN (ANALYZE, BUFFERS)` on 11 hot queries | all index-driven, <=3 ms except single-project list page 20 (9.5 ms) -> new `idx_bugs_project_updated_id` -> 0.23 ms; created by boot-time reconciliation on the existing DB | PASS |
| 43 | Load, unconstrained (app 1 worker) | `perf_load.py` 20 users, 60 s, mixed read/write | 96.7 req/s, 0 errors (41 comment 429s = rate limiter), p95 <= 480 ms, app peak 140 MB | PASS |
| 44 | Load, full stack at 0.5 CPU / 512 MB (Windows Job Object hard caps: app 0.30 CPU/320 MB, PostgreSQL 0.20 CPU/192 MB) | same | 39.4 req/s, 0 errors, p50 ~0.55 s, p95 0.65-1.16 s; app peak 111 MB, PG job 93 MB; startup 3.9 s | PASS |
| 45 | Spike, 100 users at 0.5 CPU | same, 30 s | before: 98 of ~100 requests failed; QueuePool deadlock (connections idle-in-transaction while every worker thread waited for one) | FAIL -> fixed |
| 46 | Pool deadlock fix | `get_db` async + per-loop admission semaphore (pool - 2 slots); `tests/test_db_admission.py` | burst probe 100/100 OK; spike 1255 requests, 0 errors, 39.3 req/s, p95 ~3 s, peak 120 MB | PASS |
| 47 | Soak, 10 min at 0.5 CPU / 512 MB | `perf_load.py` 20 users, 600 s | 23,934 req, 39.9 req/s, 0 errors (117 comment 429s), peak 108 MB unchanged between minute 7 and 10 | PASS |
| 48 | Compose pool (2+2) at 0.5 CPU | 20 users 60 s; 100 users 30 s | 37.5 req/s and 35.6 req/s, 0 errors | PASS |
| 49 | Cleanup | dead helpers, `API_KEY`, stale defaults/comments, ~170 plan citations, tool caches | see CLEANUP_REPORT | PASS |
| 50 | Env reference | `python scripts/gen_env_reference.py` | 125 variables, all with default and purpose; 11 settings read outside config.py were undocumented -> added to `.env.example` | PASS |
| 51 | Final frontend | `npm run lint`; `npm run test:coverage`; `npm run build`; `npm audit` | clean; 29 files / 290 tests; 17.71% lines; built; 0 vulnerabilities | PASS |
| 52 | Final browser suites | `PW_BROWSERS=chromium,firefox,webkit pytest -m ui` | 108 passed (132 s) | PASS |
| 53 | Final API fuzzing | Schemathesis `--checks all --max-examples 60`, 153 ops, 8,596 cases | 0 server errors, 0 exceptions logged; remaining 127 findings are schema-precision items (A01-A03) | PASS |
| 54 | Final PostgreSQL scenarios | `pg_upgrade_test.py`, `pg_race_test.py` | 22/22; PASS | PASS |
| 55 | Final static security | Semgrep (10 reviewed), bandit 0, hadolint, actionlint, pip-audit x2 | clean after triage | PASS |
| 56 | Clean-copy validation | copy of tracked + new non-ignored files; `scripts/run_local.ps1` from nothing; smoke; `npm ci`, lint, tests, build | launcher built `.venv` (lock-pinned) and `.env`, server healthy, smoke 30/30; frontend 290 tests; fresh build byte-identical to committed `app/static` | PASS |
