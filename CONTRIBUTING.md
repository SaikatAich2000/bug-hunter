# Contributing

Thanks for considering a contribution. This project ships as a
self-hosted internal-use tracker; PRs that keep it that shape (small,
no external dependencies, runnable on a 1 vCPU / 2 GB box) are easiest
to land.

## Setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# macOS:    source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env       # edit values you care about
```

Run the app locally:

```bash
python -m uvicorn app.main:app --reload
# browse http://127.0.0.1:8000
```

Or via Docker:

```bash
docker compose up -d
# browse http://localhost:8765
```

## Tests

The full suite must stay green for every PR.

```bash
python -m pytest -q
```

UI smoke tests use Playwright + Chromium:

```bash
python -m playwright install chromium
python -m pytest tests/test_ui_smoke.py
```

## Code style

- Match the surrounding code; this repo has consistent patterns.
- Default to **no comments** unless the *why* is non-obvious (a hidden
  constraint, a subtle invariant, a workaround for a specific bug).
- New routes get tests; new schemas get validators.
- Database changes must be **strictly additive** — see *Live-data
  safety* in [README.md](README.md). No destructive migrations.
- If you add or change an API route, regenerate the docs:
  ```bash
  python scripts/gen-api-docs.py
  ```
  (the artifacts under `docs/api/` are gitignored — the live FastAPI
  app is the source of truth.)

## Pull requests

- One concern per PR.
- Describe the change in one paragraph; list any DB or config
  implications.
- CI must pass; SonarQube quality gate must stay green.
- Reference the related issue if there is one.

## Security

Please **don't open a public issue for vulnerabilities** — see
[SECURITY.md](SECURITY.md) for the disclosure path.

## License

By submitting a contribution you agree it will be licensed under the
project's [LICENSE](LICENSE.txt).
