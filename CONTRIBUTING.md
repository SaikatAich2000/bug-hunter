# Contributing

Thanks for considering a contribution. Bug Hunter is a self-hosted tracker
designed to run on a small box with no external dependencies, so changes that
keep it small and self-contained are easiest to land.

## Setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env       # edit values you care about
```

Run the app locally:

```bash
python -m uvicorn app.main:app --reload    # http://127.0.0.1:8000
```

Or via Docker, the canonical run path:

```bash
./deploy.sh                                 # http://localhost:8765
```

### Frontend

The SPA source is in `frontend/` (React + TypeScript + Vite); the build emits
the static bundle into `app/static/`, which FastAPI serves. After changing
frontend code, rebuild so the running app picks it up:

```bash
cd frontend
npm install
npm run build
```

## Tests

The full suite must stay green for every pull request. Coverage is enforced
(`fail_under = 99`); `addopts` does not add `--cov`, so pass it explicitly:

```bash
python -m pytest -m "not ui" --cov=app      # backend suite + coverage gate
```

UI smoke tests use Playwright + Chromium and serve the built SPA, so build the
frontend first:

```bash
cd frontend && npm run build && cd ..
python -m playwright install chromium
python -m pytest -m ui
```

Static analysis is configured in `sonar-project.properties` and run with
`scripts/sonar-scan.sh` (or `scripts/sonar-scan.ps1`). It reads `coverage.xml`
and never touches the runtime database.

## Code style

- Match the surrounding code; the repo has consistent patterns.
- Default to no comments unless the *why* is non-obvious (a hidden constraint, a
  subtle invariant, or a workaround for a specific bug).
- New routes get tests; new schemas get validators.
- Database changes must be strictly additive — see *Live-data safety* in
  [README.md](README.md). No destructive migrations.
- If you add or change an API route, regenerate the docs with
  `python scripts/gen-api-docs.py` (the artifacts under `docs/api/` are
  gitignored; the live FastAPI app is the source of truth).

## Pull requests

- One concern per pull request.
- Describe the change in a short paragraph and list any DB or config
  implications.
- The test suite must pass and the SonarQube quality gate must stay green.
- Reference the related issue, if any.

## Security

Do not open a public issue for vulnerabilities — see [SECURITY.md](SECURITY.md)
for the private disclosure path.

## License

By submitting a contribution you agree it will be licensed under the project's
[MIT license](LICENSE.txt).
