<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Local development and checks

Run the app without Docker, run the tests, and know what CI checks.

> [← Back to the README](../README.md)

## Local development

### One command

```powershell
.\scripts\run_local.ps1              # Windows; add -Reload to auto-restart on code changes
```

```bash
bash scripts/run_local.sh            # macOS / Linux / Git Bash; RELOAD=1 for auto-restart
```

The first run creates `.venv` (Python 3.12, via [uv](https://docs.astral.sh/uv/)
when installed) and a `.env` with generated secrets. Set `APP_VERSION` in `.env`,
then open <http://127.0.0.1:8000> and sign in with `BOOTSTRAP_ADMIN_EMAIL` /
`BOOTSTRAP_ADMIN_PASSWORD` from `.env`. The prebuilt frontend in `app/static` is
served as-is, so Node isn't needed just to run the app.

### Backend by hand

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
python scripts/gen_local_env_secrets.py   # creates .env with generated secrets
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

On Linux you can install the exact CI pins instead with
`pip install --require-hashes -r requirements-dev-lock.txt`. The lockfile is
compiled for Linux, so it doesn't install on Windows or macOS.

Without `DATABASE_URL`, the app uses SQLite (`bug_hunter.db`) and creates the
schema on first run. API docs are at <http://127.0.0.1:8000/docs> and `/redoc`.

To use PostgreSQL instead, set
`DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/bughunter`.

### Frontend

```bash
cd frontend
npm ci               # lockfile-faithful install
npm test             # Vitest behaviour tests
npm run lint         # ESLint
npm run build        # writes the bundle into ../app/static
npm run dev          # optional Vite dev server on :5173, proxies /api (see vite.config.js)
```

Commit the rebuilt `app/static` together with frontend changes so a plain
`uvicorn` run serves the current UI. The Docker image rebuilds it anyway.

### Tests and checks

```bash
pytest -m "not ui" -n auto --cov=app         # backend suite, parallel (coverage gate: 80%)
python -m playwright install chromium        # one-time browser download (add firefox webkit if wanted)
pytest -m ui                                 # browser suites (Chromium)
PW_BROWSERS=chromium,firefox,webkit pytest tests/test_e2e_browser.py -m ui
```

Browser env vars: `PW_BROWSERS` (comma list, default `chromium`), `PW_EXECUTABLE_PATH`
(use an installed browser binary), `PW_HEADED=1` (show the window). Add
`BH_TEST_DB=postgres BH_TEST_POSTGRES_URL=...` to run the backend suite on PostgreSQL.
See [docs/testing.md](testing.md).

```bash
python -m compileall -q app tests scripts
ruff check app tests scripts
bandit -c pyproject.toml -r app -q
pip-audit -r requirements-lock.txt --strict
cd frontend && npm run lint && npm test && npm audit --omit=dev --audit-level=high
```

The browser suites start their own server on a throwaway database. Besides
the main workflows (login and deep links, every view, board drag-and-drop,
sprint configuration and reports, role gating, logout, phone width) they run
axe-core WCAG 2.1 AA checks in the dark and light themes, which needs
`npm ci` in `frontend/` first. Any uncaught page error, browser console error
or HTTP 5xx fails a test.

Load test a running instance. `scripts/load_test.py` is read-only and bounded
(credentials from `--email/--password` or `BH_LOAD_EMAIL`/`BH_LOAD_PASSWORD`);
`scripts/loadtest.py` ramps 10 to 200 users with reads and writes and needs a test
instance (see [docs/performance.md](performance.md)):

```bash
python scripts/load_test.py --base-url http://localhost:8765 --users 15 --duration 90 --docker-container bugtracker_app
```

SonarQube: `scripts/sonar-server-up.ps1` starts a local server,
`scripts/sonar-scan.ps1` / `sonar-scan.sh` scan, and
`python scripts/sonar_gate.py apply` installs the `bug-hunter-gate` quality gate
(80% coverage overall and on new code, security rating A, zero vulnerabilities).
The token comes only from the `SONAR_TOKEN` environment variable. Shell scripts keep LF
endings (`.gitattributes`). Details and the last verified results (0 bugs, 0 vulnerabilities,
0 code smells, 0 hotspots, 95.5% combined coverage, 0.5% duplication, gate passed;
Schemathesis: zero 5xx) are in [docs/testing.md](testing.md).

GitHub Actions runs on pushes and pull requests to `main`. `build-and-push.yml`
(CI): lint (compile, ruff, ESLint), backend tests with coverage on SQLite and on
PostgreSQL 16, Playwright browser tests, the frontend tests and build, then the
optional SonarQube and image-push jobs. `security.yml`: conflict markers, gitleaks,
Trivy (filesystem), bandit, hadolint, ShellCheck. `docker.yml` (pull requests):
image build plus a Trivy image scan. Dependabot proposes weekly updates. All
actions are pinned to commit SHAs. Three repository variables control the rest:

| Variable | Effect |
| --- | --- |
| `SONAR_ENABLED=true` | Run the SonarQube scan and wait for its quality gate (needs `SONAR_TOKEN` and `SONAR_HOST_URL` secrets). |
| `IMAGE_PUSH_ENABLED=true` | Build and push the image on `main` after every check passes (needs the `ACR_*` secrets). |
| `RUN_TESTS=false` | Skip the backend and frontend test jobs and the SonarQube gate for an image-only run. Lint, build and dependency audits still gate the push. |

Details per job: [docs/testing.md](testing.md).
