# Testing report

All results below come from runs on the final tree (2026-10-01, after the
Sprints redesign and two independent audit rounds) unless marked "baseline".
Raw logs are in the session scratchpad (`evidence/`).

## Verification matrix

| Category | Status | Command | Result |
|---|---|---|---|
| Static: compile | PASS | `python -m compileall -q app tests scripts` | exit 0 |
| Static: Python lint | PASS | `ruff check app tests scripts` | clean |
| Static: frontend lint | PASS | `npm run lint` (ESLint 9, react, hooks, jsx-a11y) | clean |
| Static: type checking | NOT APPLICABLE | - | the project is Python without a type checker and JavaScript (TypeScript removed by design) |
| Static: formatting | NOT APPLICABLE | - | no formatter is configured in the project |
| Static: config | PASS | `actionlint`, `hadolint`, ShellCheck, `scripts/gen_env_reference.py` | clean / reviewed |
| Unit + API (backend) | PASS | `pytest -m "not ui" --cov=app` | **2,535 passed, 0 failed** (116 files, Windows); Linux CI replication: 116/116 files pass |
| Unit (frontend) | PASS | `npm test` (Vitest 4, jsdom) | 31 files, **320 tests** passed |
| Component | PASS | Vitest component tests (MsFilter, BacklogRow, KpiStrip, PageHead, TypeTabs, SuperNav, ConfirmHost...) | included above |
| Integration (DB) | PASS | backend suite on SQLite; PostgreSQL scenario scripts | see Database |
| API fuzzing | PASS | Schemathesis 4.28 `--checks all --max-examples 60`, 153 of 159 operations, 8,596 cases | 0 server errors, 0 exceptions logged; baseline had 14 server errors |
| Functional / E2E (browser) | PASS | `PW_BROWSERS=chromium,firefox,webkit pytest -m ui` | **162 tests** (54 per engine); final run 161 passed, 1 intermittent failure (see below); with the server on PostgreSQL 16: 54/54 |
| Cross-browser | PASS | same | Chromium 153, Firefox 155, WebKit 26.6 |
| Accessibility | PASS | axe-core 4.13 WCAG 2.1 A/AA, serious+critical: every view, every Sprints tab (6) and the item dialog, dark and light, 3 browsers | 0 violations (baseline: 7 of 9 dark pages failing; the Sprints scan found nested interactive controls, fixed) |
| Responsive | PASS | 375 px viewport, list/sprints/events | no horizontal overflow |
| Smoke | PASS | `smoke.py` (30 checks: health, pages, login, CRUD, agile, reports, git config, import template, Sleuth) | 30/30 on the local instance and on the clean copy |
| Sanity | PASS | targeted suites after each change (see EVIDENCE_LOG) | - |
| Regression | PASS | full backend + frontend + browser suites on the final tree | as above |
| Negative / boundary | PASS | `tests/test_api_robustness.py` (oversized ids, duplicate names, impossible dates), Schemathesis negative phase | - |
| Security | PASS | see SECURITY_REPORT | - |
| Load | PASS | `perf_load.py` 20 users | 0 errors at 96.7 req/s unconstrained, 37-40 req/s at 0.5 CPU |
| Stress / spike | PASS | 100 users at 0.5 CPU | 0 errors after the pool fix (baseline: 98 of ~100 timed out) |
| Endurance (soak) | PASS | 10 min, 20 users, 0.5 CPU / 512 MB | 23,934 req, 0 errors, memory flat at 108 MB |
| Recovery / resilience | PASS | concurrent replica boot; degraded start when the DB is down or slow (`test_startup_degraded.py`); pool timeout path; login lockout | - |
| Database migration | PASS | `pg_upgrade_test.py` (3.1 -> 4.0 on PostgreSQL 16); `tests/test_postgres_migration.py` (legacy Sprints data); production-mode redeploy on a 31,000-issue database | 22/22; 2/2; boots, 31,011 issues re-ranked, second boot no-op |
| Idempotency | PASS | second boot changes nothing; Idempotency-Key tests; duplicate-name 409 tests; `gen_local_env_secrets.py` re-run keeps values | - |
| Clean environment | PASS | copy of tracked + new files, launcher from nothing, suites, `npm ci`, build | build byte-identical to committed `app/static` |
| Container build / deploy | PASS | `./deploy.sh` (production mode: `APP_ENV=production`, secure cookies, SMTP to a local sink) | healthy; production checks 14/14 (HSTS, CSP, docs disabled, secure cookies, no Server header...) |
| SonarQube | PASS | local SonarQube Community server, `sonar-scanner-cli`, gate `bug-hunter-gate` | gate PASSED: 0 bugs, 0 vulnerabilities, 0 hotspots, coverage 88.3% (new code 93.5%); see SONARQUBE_REPORT |
| Concurrency (PostgreSQL) | PASS | `pg_sprint_race.py`: simultaneous sprint starts and completes | 5 rounds: exactly one success each |
| Linux CI replication | PASS | the four CI jobs (lint incl. Bandit, test, frontend build incl. npm audit, dependency audit incl. pip-audit) in a Linux container | 4/4 |
| Live external integrations | BLOCKED | - | no credentials (SMTP, Firebase, GitHub, LLM providers) |

## Coverage

| Scope | Tool | Result | Gate |
|---|---|---|---|
| Backend `app/` (line + branch) | pytest-cov / coverage.py, `branch = true`, Linux CI | **93%** (16,785 statements, 1,020 missed; 4,958 branches, 368 partial) | 80% (`pyproject.toml`) - PASS |
| Frontend `src/**/*.js(x)` | Vitest v8 | 21% lines (logic modules 89-97%; views covered by the browser suites) | SonarQube overall 88.3% - PASS |

Exclusions: only `tests/`, `__pycache__` and `app/static/**` (built and
vendored assets). No production module is excluded.

Lowest backend modules: `app/agile/boards.py` 53%, `app/git/credentials.py`
54% (key-rotation and crypto-unavailable branches), `app/chatbot/llm_tools_agent.py`
56% (live-LLM paths), `app/agile/ranking.py` 57% (rebalancing edges),
`app/routes/agile.py` 61%. Agile behaviour in these modules is additionally
exercised by the browser suites and fuzzing, which coverage.py doesn't measure
(they run the app in a separate process).

The frontend is mainly verified through real-browser workflows (108 tests)
rather than unit coverage. No assertion-free or getter-only tests were added
to raise the number.

## Sprints (Jira Scrum) verification

| Area | Evidence |
|---|---|
| Hierarchy rules on every write path | `test_agile_integrity.py`, `test_agile_jira_rules.py`, `test_agile_audit_fixes.py` |
| Ranking (fuzzed) | 5,000 appends/prepends stay 6 characters; 20,000 random inserts stay sorted and unique; 400-issue database test; legacy 62-character tokens re-spaced |
| Reports against hand-computed Jira figures | `test_agile_reports_jira.py` (10), `test_agile_reports_audit.py` (16 adversarial scenarios from the independent reports audit) |
| Legacy data upgrade | `test_agile_upgrade.py` (6), PostgreSQL 16 run, production-mode database |
| Browser journeys (3 engines) | board drag, keyboard move via the card menu, backlog keyboard ranking via the drag handle, full plan/start/complete/report loop, every report renders, a cancelled create never leaks a sprint |
| Independent audits | two rounds of four reviewers (backend, reports, frontend, tests); every finding fixed and covered by a test (FINAL_CLOSURE_TRACKER C09-C21) |

Intermittent: in the final three-engine run, `test_item_dialog_has_no_serious_a11y_violations[firefox-dark]`
failed once. It passed in 150 consecutive Firefox accessibility runs and two
further full Firefox runs afterwards; the failing run's details were not
captured, so the cause is not known. Recorded in KNOWN_LIMITATIONS.

## Database

| Check | Result |
|---|---|
| Fresh install on PostgreSQL 16 | schema created, advisory lock taken, seed + board backfill under the lock |
| Legacy agile data (Collections, Features, orphan sub-tasks, Epics in sprints) | converted on first boot, second boot changes nothing (`test_postgres_migration.py`) |
| Upgrade from 3.1 with data | all rows, attachments (sha256), links, events, logins preserved; display ids backfilled; timestamps carry offsets; agile enabled on legacy projects; second boot a no-op |
| Two replicas booting at once on an empty database | 3/3 clean (was failing) |
| `clean_db.py` on PostgreSQL | baseline preserved; sequences restart at 1 |
| Query plans on 30k items | all index-driven; one index added (see PERFORMANCE_REPORT) |
| Rollback | not implemented (additive migrations only; restore from backup) - documented in README |

## Browser validation details

| Workflow | Chromium | Firefox | WebKit |
|---|---|---|---|
| Emailed deep link -> login -> item opens, hash consumed | PASS | PASS | PASS |
| Off-site `next` refused | PASS | PASS | PASS |
| Every admin view renders | PASS | PASS | PASS |
| Event deep link | PASS | PASS | PASS |
| Board drag moves card and persists status | PASS | PASS | PASS |
| Sprint lifecycle: drag into sprint, start (opens board), finish on board, complete with carry-over, sprint report figures | PASS | PASS | PASS |
| Every agile report renders (9 reports) | PASS | PASS | PASS |
| Whole browser suite with the server on PostgreSQL 16 | PASS | not run | not run |
| Regular user: no privileged nav, `/api/audit` 403 | PASS | PASS | PASS |
| Logout ends the session; back to app redirects to login | PASS | PASS | PASS |
| Description editor plain, comment editor rich | PASS | PASS | PASS |
| 375 px: list, sprints, events | PASS | PASS | PASS |
| axe WCAG AA: 9 pages x 2 themes | PASS | PASS | PASS |
| Existing smoke suites (create item, comments, account menu, session revoke, events, reports, attachments, project state) | PASS | not run | not run |

Every browser test also fails on uncaught page errors, console errors and HTTP
5xx: none occurred in the final run.

Untested combinations: mobile browsers on real devices, Safari on macOS/iOS
(WebKit on Windows is the proxy), Edge (Chromium-based).
