# Audit report and findings register

Production-readiness pass over Bug Hunter 4.0 (branch `feature/xops-parity`,
uncommitted), 2026-09-30. Scope: the whole repository (FastAPI backend, React
frontend, PostgreSQL/SQLite schema, Docker/Compose, CI, scripts, docs). The
source of product intent is the repository itself: README, DEPLOYMENT,
SECURITY, CHANGELOG, `.env.example`, tests and code. No separate requirements
document exists, so "complete" below means complete against that intent.

Raw command output lives outside the repository in the session scratchpad; the
command-by-command record is [EVIDENCE_LOG.md](EVIDENCE_LOG.md). Earlier
defects fixed while porting the xOPS Tracker features are listed in
`CHANGELOG.md` under 4.0.

## Environment limits that shaped this audit

| Capability | Available | Consequence |
|---|---|---|
| Python 3.12, Node 20, uv | Yes | Full backend/frontend toolchain |
| PostgreSQL 16 | Portable binaries in a scratch cluster | Upgrade, race, clean_db, query plans, load all ran on real PostgreSQL |
| Chromium, Firefox, WebKit | Playwright browsers | Real-browser tests on all three engines |
| Docker | **No** | Image build, Compose start-up and container health check not executed (BLOCKED) |
| SonarQube server | **No** | Quality gate not run (BLOCKED); local scanners used instead |
| LLM / Firebase / SMTP / GitHub credentials | **No** | Live integrations not exercised; mocked tests only |
| cgroups | Windows host | 0.5 CPU / 512 MB enforced with Windows Job Object hard caps instead |

## Findings register

Severity: Critical, High, Medium, Low, Info. Status per the brief: PASS (fixed
and verified), PARTIAL, BLOCKED, NOT TESTED, NOT APPLICABLE; ACCEPTED marks a
reviewed risk left as is on purpose.

| ID | Sev | Finding | Evidence | Paths | Resolution | Verification | Status |
|---|---|---|---|---|---|---|---|
| F01 | High | Connection-pool deadlock: a burst of concurrent requests exhausted the pool while all worker threads waited for a connection and the requests holding connections waited for a worker; everything stalled until the 30 s pool timeout | 100-user spike at 0.5 CPU: 98 of ~100 requests failed; PostgreSQL showed all pool connections `idle in transaction`; app CPU ~0.04 cores | `app/database.py` | `get_db` is async; a per-event-loop semaphore admits at most (pool + overflow - 2) requests to a session, waiting without holding a thread | `tests/test_db_admission.py`; burst probe 100/100 OK; spike 1,255 req, 0 errors (pool 5+5) and 1,160 req, 0 errors (Compose pool 2+2) | PASS |
| F02 | High | Startup race between replicas: a replica that skipped migration still ran first-run seeding and the board backfill, before tables existed or alongside another replica (duplicate-seed risk) | `pg_race_test.py` on empty PostgreSQL: ERROR `relation "projects" does not exist`, then `boards` missing | `app/database.py`, `app/main.py` | `init_db(on_migrated=...)`: seeding and board backfill run once, under the advisory lock, after the DDL commit | race test 3/3 clean on fresh databases; `tests/test_startup_degraded.py` | PASS |
| F03 | High | Open redirect after login: `next=/%09/evil.example` passed the `^/(?![/\\])` check; browsers strip the tab and navigate to `//evil.example` | Semgrep `js-open-redirect` + manual analysis | `frontend/src/login/LoginPage.jsx`, `frontend/src/lib/safeRedirect.js` | Resolve with `new URL()` and require the page origin | `safeRedirect.test.jsx` (12 cases); browser test `test_login_rejects_offsite_next` on 3 engines | PASS |
| F04 | Medium | Notification deep links ignored: emails, push and desktop notifications link to `/#bug=N` / `/#event=N`, but the SPA never read the hash (pre-existing since 3.x); signed-out users also lost the fragment at login | Code search: no `location.hash` reader; browser test | `frontend/src/state/AppContext.jsx`, `views/EventsView.jsx`, `lib/deepLink.js`, `lib/safeRedirect.js` | Parse after boot and on `hashchange`; login keeps a carried fragment | `deepLink.test.jsx`; browser tests for item and event links, through login | PASS |
| F05 | Medium | Timestamps without UTC offset on SQLite; browsers parse them as local time, shifting every displayed time by the viewer's offset | Schemathesis: 66 "not a date-time" violations | `app/models.py` | `UTCDateTime` column type tags naive values as UTC on read (60 columns); DDL unchanged | `test_timestamps_carry_a_utc_offset`; upgrade test checks the offset on PostgreSQL | PASS |
| F06 | Medium | HTTP 500 on integers wider than the column (path, query or body) | Schemathesis: 14 server errors, 114 `OverflowError` in the log | `app/main.py` | `OverflowError`/`DataError` -> 422 in FastAPI's validation shape | 9 cases in `tests/test_api_robustness.py`; final fuzz run 0 server errors | PASS |
| F07 | Medium | HTTP 500 on duplicate sprint name (create and rename): the service flushes inside a call that was outside the route's `try` | Schemathesis server error; `IntegrityError` in log | `app/routes/agile.py` | 409 on create, bulk create and rename | parametrized duplicate test for sprint, release, component, collection | PASS |
| F08 | Medium | Accessibility: event names rendered black on the dark theme (unstyled `<button>`); WCAG AA contrast failures in both themes; four dropdowns and the rich-text editors had no accessible name | axe-core 4.13: 7 of 9 dark-theme pages failing, light theme failing on the accent colour | `frontend/src/styles/styles.css`, `AuditView.jsx`, `BugModal.jsx`, `RichEditor.jsx` | Contrast tokens adjusted within the Steam palette; names added; accent-fill tokens per theme | 18 axe checks (9 pages x 2 themes) pass on Chromium, Firefox, WebKit | PASS |
| F09 | Medium | 15 frontend tests never executed: two test files had tests nested inside other tests (conversion damage); Vitest 1 ignored them silently | Surfaced by the Vitest 4 upgrade | `MsFilter.test.jsx`, `pushWeb.test.jsx` | Structure repaired; all files scanned for the same pattern | 290 frontend tests pass (was 248) | PASS |
| F10 | Medium | Web push retried for ~6.5 minutes when the Firebase SDK was present but blocked, although a retry can't help | Newly executed test timed out | `frontend/src/lib/push.js` | Permanent condition returns a sentinel that stops the loop | `pushWeb.test.jsx` | PASS |
| F11 | Medium | CI never ran (triggers on `dev`, which doesn't exist here), 19 actions on mutable tags, image push aimed at the xOPS registry | Workflow review; Semgrep | `.github/workflows/build-and-push.yml` | Triggers on `main`; actions pinned to SHAs; SonarQube and push opt-in via variables; least-privilege `permissions` | actionlint 1.7.12 clean | PASS |
| F12 | Medium | Frontend dev dependencies: 6 vulnerabilities (3 critical in unused `happy-dom`, esbuild/vite/vitest chain) | `npm audit` | `frontend/package.json`, lock | Removed happy-dom; Vite 6.4.3, Vitest/coverage 4.1.11; stale lock entry removed | `npm audit`: 0; lint, 290 tests, build pass | PASS |
| F13 | Medium | Descriptions are plain text (xOPS design) but the editor offered formatting that was discarded on save; 3.x formatted descriptions display flattened and are flattened on next save; undocumented | Upgrade test on PostgreSQL; code review | `RichEditor.jsx`, `BugModal.jsx`, README, CHANGELOG, DEPLOYMENT | Formatting toolbar hidden for descriptions; behaviour and upgrade effect documented. Keeping or reverting plain text is a product decision | browser test `test_description_is_plain_text_but_comments_keep_formatting`; upgrade test proves stored HTML is untouched until the next save | PARTIAL |
| F14 | Low | Daily report for an impossible date returned Python's `strptime` message | Schemathesis | `app/routes/agile_reports.py` | Route validates the date | 3 cases in `test_api_robustness.py` | PASS |
| F15 | Low | Six file endpoints (xlsx, csv, attachments) documented as JSON | Schemathesis "undocumented content type" | `app/api_docs.py`, report/bug/chat routes | File response docs | OpenAPI check; final fuzz run | PASS |
| F16 | Low | `API_KEY` passed through Compose and read by config but used nowhere: an operator could believe it protects the API | Config/reference scan | `app/config.py`, `docker-compose.yml`, tests | Removed | full suite | PASS |
| F17 | Low | `SESSION_REQUIRE_JTI`, `TRUST_PROXY_HOP_COUNT` undocumented | Generated env reference | `.env.example` | Documented | `audit/ENV_REFERENCE.md` | PASS |
| F18 | Low | Browser suites: Playwright undeclared; the sprints suite needed a hand-started server with existing data, mostly printed "[OK]" without asserting, and called `sys.exit(1)` | Suite review | `requirements-dev.txt`, `tests/test_playwright_sprints.py` | Playwright declared (lock updated: +playwright, +pyee only); suite replaced by `tests/test_e2e_browser.py` | 108 browser tests pass | PASS |
| F19 | Low | Five permission helpers and an auth dependency existed only for their own unit tests (routes enforce the same rules via `require_admin`/`require_manager_or_admin`) | vulture + reference count | `app/auth.py`, tests | Removed; the cookie-resolver test now targets the live function | auth and role-policy suites | PASS |
| F20 | Low | ~170 comments cited an unpublished xOPS planning document ("Section 6.5", "Slice 3") | grep | app, frontend, tests | Citations removed or rewritten; explanations kept | lint, compile, full suites | PASS |
| F21 | Low | Dockerfile: non-numeric `USER`, split RUN, stale comment | hadolint | `Dockerfile` | `USER 1000:1000` (same UID as before), merged RUN, documented ignores | hadolint clean; image build NOT TESTED (no Docker) | PARTIAL |
| F22 | Low | `clean_db.py` interpolated table/column names unquoted (metadata-sourced, not injectable) | Semgrep | `scripts/clean_db.py` | Dialect identifier quoting | ran against a PostgreSQL copy; sequences restart at 1 | PASS |
| F23 | Low | Load-test script defaulted to a password no install uses | Review | `scripts/load_test.py` | Requires `--password`/`BH_LOAD_PASSWORD` | ruff | PASS |
| F24 | Low | README claimed no flag can skip CI checks; `RUN_TESTS=false` skips tests and SonarQube | Workflow review | `README.md` | Documented accurately | review | PASS |
| F25 | Info | Single-project item list walked the global index (9.5 ms at page 20 on 30k items) | `EXPLAIN ANALYZE` | `app/models.py` | `idx_bugs_project_updated_id`, created at boot | 0.23 ms after | PASS |
| F26 | Low | Local launcher installed unpinned dependency versions | Clean-copy review | `scripts/run_local.*` | uv path constrained to the dev lockfile | clean copy installs the lock's exact versions | PASS |
| F27 | Low | Tool caches (`.schemathesis/`, `.hypothesis/`) not ignored | Clean-copy review | `.gitignore`, `.dockerignore` | Ignored | `git check-ignore` | PASS |
| F28 | Low | Stale pytest config comment and two unused markers | Review | `pyproject.toml` | Removed | suites run | PASS |
| A01 | Low | Business-rule 422s return `detail` as a string while OpenAPI declares FastAPI's array shape | Schemathesis (2 remaining) | app-wide | ACCEPTED: changing it alters the public error contract; the frontend handles both | documented | ACCEPTED |
| A02 | Low | Some operations don't declare 400/405/429 in OpenAPI; validators stricter than the schema (min lengths, enums) | Schemathesis | app-wide | Documentation precision only | documented | ACCEPTED |
| A03 | Info | `405` responses list only one route's methods in `Allow` (FastAPI/Starlette limitation) | Schemathesis | framework | ACCEPTED | documented | ACCEPTED |
| A04 | Medium | Frontend unit coverage 17.7% of lines | Vitest v8 coverage | `frontend/src` | Views are covered by the browser suites instead; no coverage-padding tests added | `npm run test:coverage` | PARTIAL |
| A05 | Medium | SonarQube analysis not executed | No server/token | CI | Opt-in job ready; see [SONARQUBE_REPORT.md](SONARQUBE_REPORT.md) | - | BLOCKED |
| A06 | Medium | Docker image build, Compose start-up and container health check not executed | Docker absent | Dockerfile, compose | Static checks only (hadolint, config review) | - | BLOCKED |
| A07 | Low | Live third-party integrations (Groq/OpenRouter, Firebase, SMTP, GitHub) not exercised | No credentials | integrations | Mocked tests pass; human verification steps in `HUMAN_ACTIONS.md` | - | BLOCKED |

Full list of accepted limitations: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## Related reports

- [TRACEABILITY.md](TRACEABILITY.md): features against implementation and tests
- [TESTING_REPORT.md](TESTING_REPORT.md): every test category and result
- [SECURITY_REPORT.md](SECURITY_REPORT.md)
- [PERFORMANCE_REPORT.md](PERFORMANCE_REPORT.md)
- [SONARQUBE_REPORT.md](SONARQUBE_REPORT.md)
- [DEPENDENCY_INVENTORY.md](DEPENDENCY_INVENTORY.md)
- [ENV_REFERENCE.md](ENV_REFERENCE.md)
- [CLEANUP_REPORT.md](CLEANUP_REPORT.md)
- [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)
- [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md)
- [../HUMAN_ACTIONS.md](../HUMAN_ACTIONS.md)
