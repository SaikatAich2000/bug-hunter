# Inputs only the owner can provide

Everything that could be built, run or verified on this machine has been. What
remains needs a decision, a credential, infrastructure or Git authorization.
No credential value appears in this file; each one is created at its source
and stored only in the deployment's `.env` or secret store.

Status legend: **PRODUCT DECISION REQUIRED**, **EXTERNAL CREDENTIAL REQUIRED**,
**INFRASTRUCTURE REQUIRED**, **USER INPUT REQUIRED** (authorization).

## U1. Item description format: plain text or rich text (PRODUCT DECISION REQUIRED)

4.0 follows the xOPS design: work-item descriptions are plain text; comments
keep rich text. Upgraded 3.x installations had formatted (HTML) descriptions.

What the code does now, whichever way you decide:

- On the first 4.0 boot, every 3.x description that contains HTML is copied
  once into `bugs.description_legacy_html` (under the migration lock, before
  requests are served). That copy is never overwritten, so **no 3.x formatting
  can be lost any more**, even after items are edited in 4.0. Verified on
  PostgreSQL: 36/36 formatted descriptions preserved, and the copy survives a
  4.0 save (`tests/test_legacy_descriptions.py`, upgrade script 24/24).
- The description editor shows no formatting toolbar, so nobody types
  formatting that would be discarded.

| Option | What happens | Work needed | Consequence |
|---|---|---|---|
| A. Keep plain text (current) | Nothing more | none | Formatted 3.x descriptions display flattened; originals stay in `description_legacy_html` for audit or a later restore |
| B. Restore rich text | Descriptions accept sanitized HTML again, as comments do | Remove `rich_text_to_plain` from the description validators and `_bug_to_out_dict`, re-enable the editor toolbar for descriptions, and restore `description` from `description_legacy_html` where the item was not edited after the upgrade | Diverges from xOPS; reports/exports that assume plain text need a check |
| C. Plain text now, decide later | Same as A | none now | B stays possible at any time because the originals are kept |

Recommendation: C. It is the current behaviour, loses nothing, and keeps both
paths open. Tell me "A", "B" or "C"; B is about half a day including tests.

## U1b. Retired Sprints levels: Collections and Features (CONFIRM)

The Sprints redesign follows Jira's three levels (Epic; Story, Task, Bug,
Requirement; Sub-task). The earlier preview's Collection and Feature levels
have no Jira equivalent, so they are retired:

- On the first boot, each Collection and Feature becomes a **label** (same
  name) on every issue it grouped, in that issue's own project; a Story under
  a Feature also joins that Feature's Epic. Nothing is deleted: the
  `collections` and `features` tables are left in place.
- Sub-tasks without a valid parent become Tasks; Epics leave sprints.
- Verified on SQLite and PostgreSQL 16, and a second boot changes nothing
  (`tests/test_agile_upgrade.py`, `tests/test_postgres_migration.py`).

Nothing is needed if this suits you. If you relied on Collections as
cross-project groupings, tell me: Jira's equivalent is a saved filter on a
label, which can be added.

## U2. Git authorization (USER INPUT REQUIRED)

All work is uncommitted on `feature/xops-parity`. Nothing was committed, pushed
or opened as a pull request. After reviewing `git status` / `git diff`:

```bash
git add -A
git status            # confirm .env, secrets/ and generated files are absent
git commit -m "Bug Hunter 4.0: xOPS parity, closure fixes and audit"
git push -u origin feature/xops-parity
gh pr create --base main --head feature/xops-parity --title "Bug Hunter 4.0"
```

The generated bundle under `app/static/assets/` changed names (content hashes);
the old files show as deleted, which is expected.

## U3. Production infrastructure (INFRASTRUCTURE REQUIRED)

Verified here with Docker Desktop: image build, `deploy.sh`, Compose health,
restarts, database outage and recovery, cgroup limits, a 10-minute soak and a
production-mode boot. What only the owner can supply:

| Input | Where it goes | Check after |
|---|---|---|
| Linux host with Docker Engine + Compose v2 | server | `./deploy.sh` ends with both containers healthy |
| Domain, DNS record, TLS certificate, reverse proxy | proxy -> `http://host:8765`; `.env`: `APP_BASE_URL=https://...`, `COOKIE_SECURE=true`, `APP_ENV=production`, `TRUST_PROXY_FORWARDED_FOR=true`, `TRUST_PROXY_HOP_COUNT=1` | response carries `Strict-Transport-Security`; login cookie is `Secure` |
| Production secrets | `python3 scripts/gen_local_env_secrets.py` on the server | app starts with `APP_ENV=production` (it refuses weak or placeholder values) |
| Off-host backup storage and schedule | cron + storage | a restore into a scratch database opens in the app |
| Firewall | host / cloud | only 443 (and 80 for redirects) reachable from outside |

## U4. Third-party credentials (EXTERNAL CREDENTIAL REQUIRED)

Each integration is off until configured; mocked tests cover the code paths.

| Integration | Verified here | Needs from you | Setting |
|---|---|---|---|
| SMTP email | Full path over real SMTP to a local sink: reset email delivered, https link, single-use token, no token in logs, no email for unknown addresses | provider host/port, account, app password | `EMAIL_BACKEND=smtp`, `SMTP_*`, `EMAIL_FROM` |
| Firebase web push | mocked | Firebase project, service-account JSON, VAPID key | `FIREBASE_*`, `FCM_CREDENTIALS_FILE`, `WEB_PUSH_ENABLED=true` |
| GitHub branches | mocked provider incl. race and retry cases | fine-grained token per repository | per project in the UI; `GIT_BRANCH_CREATION_ENABLED=true` |
| Sleuth cloud LLM | mocked | Groq and/or OpenRouter key | `SLEUTH_CLOUD_ENABLED=1`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` |
| Hosted SonarQube | local SonarQube Community run with the project's own gate (see `SONARQUBE_REPORT.md`) | server URL and token for CI | repo secrets `SONAR_HOST_URL`, `SONAR_TOKEN`; variable `SONAR_ENABLED=true` |
| Container registry | image builds locally | registry and push credentials | secrets `ACR_*`; variable `IMAGE_PUSH_ENABLED=true` |
| OTLP collector | not configured | collector URL and auth header | `OTEL_EXPORTER_OTLP_*` |

Google Gmail, Calendar and Drive connectors appeared in this session's tool
list; they are not dependencies of the application: NOT APPLICABLE.

## U5. Checks only the owner can do

| Item | Why |
|---|---|
| Confirm the sample keys in `tests/test_sleuth_memory_redaction.py` history are fake | one historic value matches a real key format; it was deliberately not printed |
| Restore `D:\Coding\xOPS_Tracker\.git` from a fresh clone | the metadata folder disappeared during the earlier port work; files are intact |
