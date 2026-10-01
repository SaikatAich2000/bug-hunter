# Dependency and external service inventory

Versions are the ones pinned in `requirements-lock.txt` / `frontend/package-lock.json`
and installed in the verified environments. Licenses come from the installed
package metadata. Vulnerability status (2026-09-30): pip-audit and npm audit
report none.

## Services

| Component | Purpose | Mandatory | Version | License | Hosting / install | Ports | Configuration | Storage and backup | Health | Without it |
|---|---|---|---|---|---|---|---|---|---|---|
| PostgreSQL | Primary database | Yes in production (SQLite only for local/dev) | 16 (`postgres:16-alpine` in Compose; tested with 16.13) | PostgreSQL License | Compose service `db`, or a managed PostgreSQL via `DATABASE_URL` | 5432 in-network; host `127.0.0.1:55432` for admin only | `POSTGRES_USER/PASSWORD/DB`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, timeouts | volume `bugtracker_pgdata`; back up with `pg_dump` (README) | `pg_isready` healthcheck; `/api/health` reports `database` | App starts degraded (`/api/health` 503) |
| SMTP server | Notification, digest and password-reset email | No | any | - | external provider | 587 (STARTTLS) or 465 | `EMAIL_BACKEND=smtp`, `SMTP_*`, `EMAIL_FROM` | - | startup log; send errors logged | Emails go to the log (`console`) or nowhere (`disabled`); password reset needs email |
| Firebase Cloud Messaging | Browser push | No | Firebase Admin SDK 7.6.0 / Web SDK 12.14.0 (self-hosted) | Apache-2.0 | Google-hosted | outbound 443 | `WEB_PUSH_ENABLED`, `FIREBASE_*`, `FCM_CREDENTIALS_FILE` or `_JSON` | service-account JSON is a secret | `/api/push/config` | No push; in-app bell and email still work |
| GitHub / GitHub Enterprise REST API | Story feature branches | No | REST v3 | - | GitHub-hosted or GHE | outbound 443 | `GIT_BRANCH_CREATION_ENABLED`, `GIT_BRANCH_DELETION_ENABLED`, `GIT_CREDENTIAL_ENCRYPTION_KEY`, per-project token | tokens Fernet-encrypted in the database; keep the key backed up | project "Test connection" | Git panel reports "not configured" |
| Groq / OpenRouter | Sleuth cloud LLM layer | No (off by default) | chat APIs | - | provider-hosted | outbound 443 | `SLEUTH_CLOUD_ENABLED`, `GROQ_*`, `OPENROUTER_*` | - | errors logged; rule engine answers | Sleuth uses local rules/classifier only |
| Google Gemini embeddings + ChromaDB | Sleuth RAG | No | not in requirements (install separately) | Apache-2.0 (chromadb) | local library + provider API | outbound 443 | `SLEUTH_RAG_ENABLED`, `GEMINI_*`, `SLEUTH_RAG_DIR` | `.sleuth_rag/` index (rebuildable) | - | RAG off |
| llama.cpp (`llama-cpp-python`) + GGUF model | Sleuth local LLM | No | not in requirements | MIT | local | - | model file in `models/` | model file (large, not in git) | - | layer dormant |
| OpenTelemetry collector (e.g. SigNoz) | Traces, metrics, logs | No | OTLP/gRPC | - | self-hosted or SaaS | outbound 4317 | `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_*` | collector-side | startup log `OTLP export enabled` | console logs only |
| Have I Been Pwned range API | Breached-password check | No (on by default) | k-anonymity API | - | api.pwnedpasswords.com | outbound 443 | `PASSWORD_BREACH_CHECK_ENABLED` | - | failures are non-blocking | check skipped |
| SonarQube | CI quality gate | No | server + `SonarSource/sonarqube-scan-action` v6.0.0 (pinned) | LGPL-3.0 (Community) | self-hosted | - | repo variable `SONAR_ENABLED`, secrets `SONAR_TOKEN`, `SONAR_HOST_URL` | - | gate status | CI skips the scan |
| Azure Container Registry | Image registry for CI push | No | - | - | Azure | - | repo variable `IMAGE_PUSH_ENABLED`, secrets `ACR_*` | - | - | CI builds but doesn't push |

## Backend runtime libraries (direct)

| Package | Version | License | Role |
|---|---|---|---|
| fastapi | 0.141.1 | MIT | web framework |
| uvicorn[standard] | 0.53.0 | BSD-3-Clause | ASGI server |
| sqlalchemy | 2.0.54 | MIT | ORM |
| pydantic | 2.13.5 | MIT | validation |
| psycopg[binary] | 3.3.6 | **LGPL-3.0-only** | PostgreSQL driver (used unmodified as a separate library, which LGPL permits) |
| python-dotenv | 1.2.3 | BSD-3-Clause | `.env` loading |
| httpx | 0.28.1 | BSD-3-Clause | outbound HTTP |
| python-multipart | 0.0.32 | Apache-2.0 | uploads |
| bcrypt | 5.0.0 | Apache-2.0 | password hashing |
| itsdangerous | 2.2.0 | BSD | signed cookies |
| tzdata | 2026.4 | Apache-2.0 | IANA zones in slim images |
| Pillow | 12.3.0 | MIT-CMU | EXIF stripping |
| truststore | 0.10.4 | MIT | OS trust store for TLS |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | Fernet encryption |
| openpyxl | 3.1.5 | MIT | Excel import/export |
| firebase-admin | 7.6.0 | Apache-2.0 | push |
| opentelemetry-* | 1.45.0 / 0.66b0 | Apache-2.0 | telemetry |

Dev-only (not in the image): pytest, pytest-order, pytest-cov, coverage, ruff,
bandit, pip-audit, playwright 1.63.0 (Apache-2.0).

## Frontend

| Package | Version | License | Shipped |
|---|---|---|---|
| react / react-dom | 18.3.1 | MIT | yes |
| @dnd-kit/core, sortable, modifiers, utilities | 6.3.1 / 10.0.0 / 9.0.0 / 3.2.2 | MIT | yes |
| dompurify | 3.4.14 | MPL-2.0 OR Apache-2.0 | yes |
| firebase (compat SDK, self-hosted in `app/static/vendor`) | 12.14.0 | Apache-2.0 | yes |
| vite | 6.4.3 | MIT | build only |
| vitest / @vitest/coverage-v8 | 4.1.11 | MIT | dev |
| eslint (+ react, hooks, jsx-a11y plugins) | 9.39 | MIT | dev |
| axe-core | 4.13.0 | MPL-2.0 | dev (accessibility tests) |
| jsdom, Testing Library | 24.1 / 14-6 | MIT | dev |

## Dependency changes in this pass

| Change | Reason | Verification |
|---|---|---|
| removed `happy-dom` (dev) | unused; three critical advisories | npm audit 0; tests pass |
| vite 5.4 -> 6.4.3, vitest 1.6 -> 4.1.11, @vitest/coverage-v8 -> 4.1.11 (dev) | esbuild dev-server and `@vitest/mocker` advisories | lint, 290 tests, coverage, build identical to a clean build |
| `npm audit fix` (brace-expansion, transitive dev) | high-severity ReDoS advisory | npm audit 0 |
| added `axe-core` (dev) | accessibility tests | pinned in lock |
| added `playwright` to `requirements-dev.txt` | browser suites imported it undeclared | dev lock regenerated with existing pins preserved (+playwright 1.63.0, +pyee 13.0.1) |

No runtime dependency was upgraded. Deprecated or abandoned packages: none
found among direct dependencies; `starlette.testclient` warns that `httpx`
support is deprecated in favour of `httpx2` (test-only, see KNOWN_LIMITATIONS).
