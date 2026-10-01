# Production-readiness report

## Verdict: CONDITIONALLY READY

Everything that could be executed on this machine passes, including real
PostgreSQL, three real browsers and enforced 0.5 CPU / 512 MB limits. The
conditions are the items this environment could not verify and the operator
steps in [HUMAN_ACTIONS.md](../HUMAN_ACTIONS.md).

| Question | Answer |
|---|---|
| Installs from a clean environment? | Yes: a copy with only tracked and new files, `scripts/run_local.ps1` from nothing, backend 2,405/2,405, frontend `npm ci` + 290 tests + byte-identical build |
| Application starts? | Yes, on SQLite and PostgreSQL 16; 3.9-4.7 s under the CPU cap on 30k items; two replicas can boot together |
| Critical workflows work? | Yes: 13 browser workflows on Chromium, Firefox and WebKit; 30-step API smoke; 3.1 -> 4.0 upgrade with data |
| Target resource profile verified? | Yes for 0.5 CPU / 512 MB via Windows Job Object hard caps (not cgroups); HDD-like storage NOT TESTED |
| Human actions remaining? | Yes: see HUMAN_ACTIONS (Docker host, TLS, secrets, backups, integrations, CI variables, one product decision) |

## Conditions before calling it production-ready

1. **Build and start the image on a Docker host** (`./deploy.sh`) and confirm
   `/api/health` and `docker compose ps` (HUMAN_ACTIONS 3). Scan the image (17).
2. **Configure HTTPS, secrets, firewall and backups** (HUMAN_ACTIONS 4-9).
3. **Decide on plain-text vs rich-text descriptions** before upgrading a 3.x
   database with formatted descriptions (HUMAN_ACTIONS 2).
4. **Run SonarQube** if the quality gate is part of your release policy
   (HUMAN_ACTIONS 15-16).

## Verification summary

| Area | Status | Evidence |
|---|---|---|
| Backend tests | PASS | 2,405 passed (clean copy); coverage 91% |
| Frontend tests | PASS | 290 passed; lint clean |
| Browser E2E + accessibility | PASS | 108 passed on 3 engines; 0 WCAG AA serious/critical |
| API fuzzing | PASS | 0 server errors in 8,596 generated requests |
| Security scans | PASS | bandit 0, Semgrep reviewed, pip-audit 0, npm audit 0, secrets clean |
| Database | PASS | upgrade 22/22, replica race 3/3, query plans indexed |
| Performance | PASS | 0 errors under load, spike and 10-min soak at 0.5 CPU / 512 MB |
| Clean environment | PASS | see above |
| Container build | BLOCKED | no Docker |
| SonarQube | BLOCKED | no server |
| Live integrations (SMTP, FCM, GitHub, LLM) | BLOCKED | no credentials; mocked tests pass |
| Frontend unit coverage | PARTIAL | 17.7% lines; covered by browser tests instead |

## Remaining gaps

| Gap | Status | Reason | Impact | Next action | Blocks production? |
|---|---|---|---|---|---|
| Docker image build / Compose start / image scan | BLOCKED | no Docker here | unverified packaging on the target | HUMAN_ACTIONS 3 and 17 | Yes, until done once |
| SonarQube gate | BLOCKED | no server | no Sonar metrics | HUMAN_ACTIONS 16 | Only if your policy requires it |
| Live SMTP / FCM / GitHub / LLM | BLOCKED | no credentials | integration behaviour against real providers unverified | configure and follow each "Verify" step in HUMAN_ACTIONS | No (all optional except SMTP for password reset) |
| Plain-text descriptions | PARTIAL | product decision | 3.x formatting flattened on next save | HUMAN_ACTIONS 2 | Only for 3.x upgrades with formatted descriptions |
| Frontend unit coverage | PARTIAL | views tested in browsers instead | lower unit-level safety net | add unit tests with future changes | No |
| HDD-like storage | NOT TESTED | NVMe host | latency on slow disks unknown | measure on the target host | No |
| Multi-worker / multi-replica rate limits and scheduler | ACCEPTED | per-process design | limits multiply; possible duplicate digests | keep one worker (as shipped) | No |
| OpenAPI precision (422 shape, some undeclared codes) | ACCEPTED | public contract | client generators see a looser schema | optional future cleanup | No |

## What changed in this pass

See [AUDIT_REPORT.md](AUDIT_REPORT.md) (F01-F28) and `CHANGELOG.md` 4.0.
Highlights: a connection-pool deadlock that froze the app under bursts, a
multi-replica startup race, an open redirect, notification links that never
worked, timestamps shown in the wrong time zone, 500s on edge inputs,
accessibility failures (including invisible event names), 15 tests that never
ran, CI that could never run, and six vulnerable dev dependencies.
