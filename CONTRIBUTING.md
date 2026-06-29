# Contributing

Thanks for your interest. Bug Hunter runs on a small server with no external
dependencies — keep changes small and self-contained and they will merge faster.

## Setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env       # edit values you care about
```

Run locally:

```bash
python -m uvicorn app.main:app --reload    # http://127.0.0.1:8000
```

Or via Docker (the canonical run path):

```bash
./deploy.sh                                 # http://localhost:8765
```

### Frontend

The SPA source lives in `frontend/` (React + TypeScript + Vite). The build
writes the static bundle to `app/static/`, which FastAPI serves. After any
frontend change, rebuild:

```bash
cd frontend
npm install
npm run build
```

## Tests

All tests must pass on every pull request. Coverage is enforced (`fail_under = 99`).
`addopts` does not include `--cov`, so pass it manually:

```bash
python -m pytest -m "not ui" --cov=app      # backend suite + coverage gate
```

UI smoke tests run Playwright against Chromium and require the built SPA:

```bash
cd frontend && npm run build && cd ..
python -m playwright install chromium
python -m pytest -m ui
```

## Code style

- Match the surrounding code.
- Only add comments when the *why* is not obvious — a hidden constraint, a
  subtle rule, or a workaround for a specific bug.
- New routes need tests; new schemas need validators.
- Database changes must only *add* — see *Live-data safety* in
  [README.md](README.md). No destructive migrations.
- If you add or change an API route, regenerate the docs with
  `python scripts/gen-api-docs.py` (artifacts under `docs/api/` are
  gitignored; the live FastAPI app is the source of truth).

## Pull requests

- One concern per pull request.
- Write a short description of the change and list any DB or config implications.
- Tests must pass and coverage must stay at or above the gate.
- Reference the related issue if one exists.

## Security

Do not open a public issue for vulnerabilities. See [SECURITY.md](SECURITY.md)
for the private disclosure path.

## License

By submitting a contribution you agree it will be licensed under the project's
[MIT license](LICENSE.txt).
