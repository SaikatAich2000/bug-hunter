# Security report

Scope: application code, frontend, dependencies, container and CI definitions.
Dynamic testing ran only against local throwaway instances. No third-party
system was tested.

## Scanners executed (final tree)

| Tool | Scope | Result | Status |
|---|---|---|---|
| bandit (pyproject config) | `app/` | 0 findings | PASS |
| Semgrep 1.178 (`p/python`, `p/javascript`, `p/react`, `p/secrets`, `p/dockerfile`, `p/github-actions`; 274 rules, 202 files) | app, frontend, scripts, Dockerfile, compose, workflows, shell | baseline 30 -> 10, all triaged below; 1 rule timed out on `app/models.py` (a system-call rule; the file has no system calls) | PASS |
| pip-audit (hash-pinned locks, `--disable-pip --no-deps`) | `requirements-lock.txt`, `requirements-dev-lock.txt` | No known vulnerabilities | PASS |
| npm audit | full tree and `--omit=dev` | 0 vulnerabilities (baseline: 6, three critical) | PASS |
| detect-secrets + real-key regexes | 357 tracked/new files, git history | only placeholders and deliberate test fixtures | PASS (one history item left to the owner, see HUMAN_ACTIONS) |
| hadolint 2.15.1 | Dockerfile | clean (2 documented ignores) | PASS |
| actionlint 1.7.12 | CI workflow | clean | PASS |
| ShellCheck 0.11 | deploy.sh, down.sh, scripts/*.sh | 3 x SC2015 info, reviewed benign | PASS |
| Schemathesis 4.28 (`--checks all`, incl. `ignored_auth`) | live API, 153 of 159 operations, 8,596 cases | 0 server errors; no endpoint accepted unauthenticated access | PASS |
| axe-core 4.13 | 9 pages x 2 themes x 3 browsers | 0 serious/critical WCAG issues | PASS |
| Trivy (image `bugtracker_app`, HIGH+CRITICAL, closure pass) | OS and Python packages in the built image | 0 critical; 0 Python; 52 high in Debian packages with no fix published (51 "affected", 1 "fix_deferred"); every fixable one removed by the build-time `apt-get upgrade` (was 58, incl. OpenSSL CVE-2026-75804) | PASS (residual risk below) |
| Production-mode boot (closure pass) | Compose stack with `APP_ENV=production` | unsafe settings refused at start-up (names only, no values logged); with safe settings 24/24 checks: HSTS, CSP and frame headers, no `Server` header, `/docs` `/redoc` `/openapi.json` 404, `Secure; HttpOnly; SameSite` session cookie, reset email over SMTP with https link, single-use token, token absent from logs, identical response for unknown emails | PASS |
| SonarQube | - | no server | BLOCKED |

## Findings fixed in this pass

| Finding | Severity | Fix | Regression test |
|---|---|---|---|
| Open redirect after login via tab-smuggled `next` (`/%09/evil.example`) | High | same-origin resolution with `URL` (`frontend/src/lib/safeRedirect.js`) | `safeRedirect.test.jsx`, `test_login_rejects_offsite_next` (3 browsers) |
| Pool deadlock: any burst of concurrent requests (unauthenticated login attempts included) could freeze the app for 30 s at a time, a cheap denial of service | High | admission-controlled `get_db` | `test_db_admission.py`, spike tests |
| HTTP 500 on oversized integers (error surface, noisy logs) | Medium | 422 handler | `test_api_robustness.py` |
| Python exception text returned to clients (daily report date) | Low | clean 422 | `test_api_robustness.py` |
| 19 CI actions on mutable tags (supply-chain) | Medium | pinned to commit SHAs, `permissions: contents: read` | actionlint |
| Critical advisories in dev dependency `happy-dom` (unused) and the Vite/Vitest chain | Medium | removed / upgraded | npm audit 0 |
| Dead `API_KEY` setting suggested API-key protection that doesn't exist | Low | removed | full suite |
| `clean_db.py` unquoted identifiers | Low | dialect quoting | PostgreSQL run |
| `Server: uvicorn` sent by the container although the middleware "removes" it (uvicorn adds it below the ASGI app) | Low | `--no-server-header` in the image `CMD` and local launchers | `test_image_does_not_advertise_the_server`; live check on the rebuilt image |
| Fixable OpenSSL advisory in the base image (the `python:3.12-slim` tag lagged the Debian fix) | Medium | build-time `apt-get upgrade` | Trivy: no fixable finding left |
| Image shipped dependency versions CI never tested (runtime lock drifted from the dev lock: psycopg, SQLAlchemy, tzdata, uvicorn) | Medium | runtime lock compiled with the dev lock as constraints | `test_runtime_lock_ships_the_versions_ci_tests` |

## Remaining Semgrep results (reviewed, not defects)

| Rule | Location | Review |
|---|---|---|
| `python-logger-credential-disclosure` | `app/git/credentials.py` x4 | Logs only `type(exc).__name__`; never the key, token or ciphertext. |
| `python-logger-credential-disclosure` | `app/fcm_transport.py:152`, `app/push_service.py:82` | Logs FCM error text and user ids, no tokens or credentials. |
| `insecure-hash-algorithm-sha1` | `app/password_breach.py:35` | Required by the Have I Been Pwned k-anonymity protocol (only a 5-char SHA-1 prefix leaves the server); not used for integrity or passwords. |
| `react-dangerouslysetinnerhtml` | `BugModal.jsx` (comments), `SleuthPanel.jsx` | Server sanitizes on write (allowlist, forced `rel="noopener nofollow"`), DOMPurify on render; Sleuth text is escaped first and uses a strict tag allowlist. |
| `avoid-sqlalchemy-text` | `scripts/clean_db.py` | Identifiers come from ORM metadata and are now quoted; the only value is a bound parameter. |

## Application security review

| Area | Implementation | Evidence |
|---|---|---|
| Passwords | bcrypt; min length + letter/number; common-password denylist; optional HIBP k-anonymity check | `test_password_policy.py`, `test_auth_unit.py` |
| Sessions | signed cookie, `HttpOnly`, `SameSite=Lax`, `Secure` on HTTPS; server-side session rows with per-device revocation; password change bumps `session_version` | live header capture; `test_auth_routes.py`, `test_security.py` |
| Brute force | per-IP rate limit on login (8/min) and per-account lockout | `test_security.py`, `test_auth_routes.py` (lockout); 429s observed during load tests |
| Authorization | role dependencies (`require_admin`, `require_manager_or_admin`), per-type edit policy (also on every Agile write), project scoping for non-admins | `test_role_policy.py`, `test_project_access.py`, `test_query_scoping.py`, `test_chat_authz_parity.py`; Schemathesis `ignored_auth`: no violations |
| CSRF | Origin/Referer check on unsafe methods plus SameSite cookies | live: cross-origin POST -> 403; `test_security.py` |
| XSS | CSP `script-src 'self'` (no inline), server allowlist sanitizer, DOMPurify | live headers; `test_richtext_forms.py`; axe/browser runs show no console errors |
| Headers | CSP, X-Frame-Options DENY, nosniff, Referrer-Policy, Permissions-Policy, COOP/CORP, HSTS when `COOKIE_SECURE` | live capture (see TESTING_REPORT) |
| Input limits | request body cap (`MAX_REQUEST_BODY_BYTES`), bounded list sizes, 422 on out-of-range ids | `test_hardening*.py`, fuzzing |
| Uploads | size limits, content-type allowlist, EXIF stripping, safe download content types | `test_export_attachment_safety.py` |
| SSRF | Git provider base URL validation (no credentials, scheme/host rules), TLS verification can't be disabled | `test_git_tls_and_urls.py` |
| SQL injection | ORM everywhere; LIKE input escaped | `test_query_safety.py` |
| Secrets | none committed; Git tokens Fernet-encrypted at rest; log redaction for OTLP export; secret-redaction filter before any LLM call | secret scans; `test_sleuth_memory_redaction.py`, `test_telemetry.py` |
| Production guard | start-up refuses weak/placeholder secrets, auto-login, malformed Fernet key; strict tier for `APP_ENV=production` | `test_hardening_extra.py` |

## AI assistant (Sleuth)

| Risk | Control | Evidence |
|---|---|---|
| Prompt injection -> unauthorized action | Every tool re-checks the caller's own permissions server-side; writes need explicit user confirmation; no delete tool | `test_sleuth_tools_direct.py`, `test_agile_slice6_and_sleuth_llm_tools.py` |
| Data exfiltration to the model | Cloud layer off by default; secret-redaction filter on all text sent; answers scoped to the caller's projects | `test_sleuth_memory_redaction.py`, `test_chat_authz_parity.py` |
| Cross-user memory | per-user conversation memory | `test_sleuth_*` |
| Cost / abuse | 30 requests/min/user; bounded tool rounds and tokens | `app/chatbot/router.py`, config |
| Live model behaviour | needs a Groq/OpenRouter key | EXTERNAL CREDENTIAL REQUIRED |

## Residual risks

- 52 high-severity advisories in Debian base packages have no fix published
  yet (none in Python packages, none critical). Most concern local tools
  (util-linux, login, ncurses) that the app never invokes; `curl` is used only
  by the health check against 127.0.0.1. Rebuild the image regularly.
- Dark-theme primary buttons use white text on the Steam blue gradient; axe can't
  measure gradients and the colours are part of the preserved theme (KNOWN_LIMITATIONS).
- Rate limiters and the lockout counter are per process: with more than one app
  worker or replica the limits multiply. The shipped Compose runs one worker.
- `SESSION_REQUIRE_JTI` is off by default for 3.x cookie compatibility; turn it
  on after the upgrade window (HUMAN_ACTIONS).
