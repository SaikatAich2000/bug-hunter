<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Security and quality testing

> [← Back to the README](../README.md)

How Bug Hunter was tested before public launch, what was found, and how to run each check again.
The testing was done against local copies, never a deployment. Every dismissed finding was
reproduced by hand first, with a one-line reason.

## Method and tools

| Area | Tools (all free) | Scope |
|---|---|---|
| Static analysis | Semgrep (OWASP Top 10, Python, JavaScript, React, secrets, Dockerfile packs), Bandit, ruff, ESLint | whole repository |
| Dependencies | pip-audit, npm audit, OSV-Scanner, Trivy (filesystem and image), Grype | lockfiles and the built image |
| Secrets | Gitleaks, TruffleHog (full git history) | every commit |
| Images and config | Hadolint, Checkov (Dockerfile, workflows, YAML) | Dockerfile, CI, compose |
| Dynamic | OWASP ZAP (passive and authenticated active scans, one run per role), Nuclei, sqlmap, Schemathesis | running instance |
| Browser | Playwright (Chromium, Firefox, WebKit: every browser test on all three), axe-core, `scripts/qa/ui_audit.py` | every public page and signed-in view at 7 widths |
| Database | PostgreSQL 16 full test suite, pgTAP schema tests, upgrade from a real older release, backup and restore drill, Toxiproxy outage test | schema, data safety, resilience |
| Load | k6 (smoke, load, stress, spike, 30-minute soak), py-spy profile | the Compose stack at its real resource limits |
| Test quality | mutation testing of the security modules (Python and frontend), Hypothesis property tests | access control, SSRF guard, upload policy, billing money parsing, sanitizers, redirects |

## Results

| Check | Result |
|---|---|
| Authentication | Lockout after 6 wrong passwords, correct password refused during lockout, no account enumeration (same answer and, since this audit, same work for known and unknown addresses), reset and signup throttled, 25 MB body gets 413 |
| SQL injection | sqlmap at level 5 / risk 3 with every technique against the search, filter and project-update parameters on PostgreSQL: none injectable. ZAP's one SQL-injection alert (`PUT /api/projects/{id}`, `name`) was a false positive: six injection payloads were stored verbatim |
| Dynamic scans | ZAP active scans as admin, manager and user: no exploitable finding. Remaining alerts are dev-only or accepted (see below) |
| HTTPS | Behind a TLS proxy: `__Host-` session cookie (HttpOnly, Secure, SameSite), HSTS two years with subdomains, COOP and CORP same-origin, restrictive Permissions-Policy, nosniff, frame denial |
| Images | Trivy and Grype: no fixable high or critical issue; pip in the base image upgraded in the Dockerfile |
| Database | pgTAP (tables, columns, keys, uniqueness, checks, indexes, tenancy columns), upgrade from an older release loses no rows and ends with a schema identical to a fresh install, backup and restore leaves every table byte-identical, a 30-second database outage returns 503 and the app recovers on its own with no lost or duplicated writes |
| Load (0.3 vCPU, 320 MB app) | about 27-38 requests/s sustained; beyond that latency rises, nothing fails; 30-minute soak: memory flat, p95 143 ms at 20 users |

### Found and fixed during the audit

- Bulk import of a CSV with a stray carriage return or NUL byte answered **500**; it now answers 400 (`app/bulk_import.py`).
- A forgotten-password request did its database writes inside the request for a real account only, a timing difference; all writes and the e-mail now run after the response, so both cases cost one lookup (`app/routes/auth.py`).
- No general limit on API traffic from one address; added `API_RATE_MAX` (default 1200 per window, `0` turns it off).
- A lost database answered 500; it now answers **503 with `Retry-After`**.
- 30 foreign keys had no index, so deleting a user scanned whole tables (338 ms at 300 000 rows, 9 ms after); every foreign key now gets an index automatically (`_index_foreign_keys` in `app/models.py`).
- The outbound-request guard in the single-tenant edition lacked 6to4 unwrapping and the non-global check; both editions now share the same guard.
- The personal data export is served as a download (`Content-Disposition: attachment`).
- Images and fonts were sent as `application/octet-stream` in the slim container image (it has no `/etc/mime.types`, so Python did not know `.webp`, `.woff2`, `.woff`), which together with `nosniff` makes WebKit (Safari, every browser on iOS) refuse them; the types are now registered explicitly and a test checks that every file type under `app/static` has one. Found by running the browser suites on WebKit.
- The mobile menu now closes on the Escape key; the project and user filter controls in the sidebar have a 24 px target (WCAG 2.5.8); the legal-page footer links have room to tap.
- Dependency updates (pillow, certifi, DOMPurify), pinned GitHub Actions in the Android repository, a pip upgrade in the Dockerfile, a dependency-update cooldown.

### Accepted or by design

- `style-src 'unsafe-inline'`: the single-page app sets inline styles. `script-src` has no inline or eval.
- ZAP alerts about `/openapi.json` ("Internal Server Error" text), `/api/push/config` (public Firebase web configuration, empty unless push is set up), path traversal on stored text fields (no file access), numeric IDs in URLs: all false positives, and the API documentation is off in production.
- Text search is a `LIKE` scan: about 110 ms at 100 000 items, growing with the table. Past roughly 500 000 items add a trigram index (`CREATE EXTENSION pg_trgm; CREATE INDEX ... USING gin (lower(title) gin_trgm_ops)`).
- Rate limits and lockout are per process, so each replica counts separately.

## ASVS 5.0 (level 2) mapping

A map of where each chapter of the OWASP Application Security Verification Standard 5.0 is covered. It is a self-assessment, not a certification.

| Chapter | Evidence | Status |
|---|---|---|
| V1 Encoding and sanitization | server allowlist sanitizer plus DOMPurify, parameterized queries, Excel formula defanging, property tests | met |
| V2 Validation and business logic | Pydantic validation, check constraints, money parsing property tests, bulk import row validation | met |
| V3 Web frontend security | CSP, `nosniff`, frame denial, COOP/CORP, no inline script, safe redirects | met, `unsafe-inline` for styles accepted |
| V4 API and web service | OpenAPI-driven fuzzing (Schemathesis), role matrix, rate limits, body limits, 4xx/5xx shapes | met |
| V5 File handling | extension plus magic-byte check, size limits, image metadata stripping, download disposition | met |
| V6 Authentication | bcrypt with pre-hash, breached-password check, TOTP and recovery codes, lockout, reset and e-mail-change flows | met |
| V7 Session management | server-side session ids, revocation, rotation on password change, cookie flags | met |
| V8 Authorization | single access function, cross-tenant and role-matrix tests, mutation-tested | met |
| V9 Self-contained tokens | no JWTs; session tokens are signed and encrypted and also checked against the database | not applicable |
| V10 OAuth and OIDC | the app is not an OAuth provider or client | not applicable |
| V11 Cryptography | Fernet and AES-GCM for secrets at rest, keys from environment, no custom primitives | met |
| V12 Secure communication | HSTS, TLS terminated by the platform, outbound HTTPS only, SSRF guard on webhooks and Git | met |
| V13 Configuration | production start-up checks refuse unsafe settings, secrets only in the environment, pinned and scanned dependencies | met |
| V14 Data protection | export and deletion of personal data, retention jobs, masked e-mail in logs | met |
| V15 Secure coding and architecture | lockfiles with hashes, SAST in CI, small trust boundaries documented in [security-model.md](security-model.md) | met |
| V16 Logging and error handling | audit trail, generic error bodies with a request id, no secrets in logs | met |
| V17 WebRTC | not used | not applicable |

## Running it again

```bash
# unit, API, browser and database suites
pytest -m "not ui" -n auto            # backend
pytest -m ui                          # browser suites; PW_BROWSERS=chromium,firefox,webkit for all engines
cd frontend && npm test && npm run lint
BH_TEST_POSTGRES_URL=postgresql+psycopg://postgres:<pw>@127.0.0.1:5432/postgres pytest tests/test_postgres_migration.py

# fast smoke test of a running instance (about 20 requests)
hurl --test --variable base=http://127.0.0.1:8000 --variable email=<admin> --variable password=<pw> scripts/qa/smoke.hurl

# schema tests on PostgreSQL with pgtap
python scripts/qa/gen_pgtap.py && scripts/qa/run_pgtap.sh postgresql+psycopg://postgres:<pw>@127.0.0.1:5432/<empty db>

# load (see the header of the script for the stages)
docker run --rm -i -v "$PWD/scripts/qa/k6:/s" grafana/k6 run -e STAGE=load -e BASE_URL=http://host.docker.internal:8765 -e EMAIL=<admin> -e PASSWORD=<pw> /s/mixed.js

# responsive and accessibility sweep of a running instance
python scripts/qa/ui_audit.py http://127.0.0.1:8000 <admin> <pw> all

# a large synthetic dataset for query-plan work
DATABASE_URL=postgresql+psycopg://... python scripts/qa/seed_scale.py
```

Raise `RATE_LIMIT_SCALE` and set `API_RATE_MAX=0` on a throwaway instance before load or scanner runs, or the per-address limits will answer 429.
