# Changelog

All notable changes to Bug Hunter. The format follows
[Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Fixed

- **Email digest fired at the wrong local time.** The slim Docker image ships no
  IANA timezone data, so `EMAIL_DIGEST_TIMEZONE` silently fell back to UTC and a
  `0 8 * * *` schedule ran at 08:00 UTC instead of 08:00 local. The `tzdata`
  package is now a pinned dependency; rebuild the image (`./deploy.sh`) to pick
  it up.
- **A failed digest send lost that day's emails.** Rows were stamped as emailed
  before the SMTP send; a delivery failure was ignored. Failed sends are now
  released and retried on the next scheduled run, with an error in the log.
  At-most-once delivery is preserved.

### Changed

- `EMAIL_DIGEST_LOOKBACK_HOURS` default raised from 26 to 50 (twice the daily
  gap), so one missed or failed run is fully caught up by the next. A wider
  window can never double-send.
- The startup log now warns loudly when digest mode is enabled without a
  schedule (`EMAIL_DIGEST_CRON` empty means no work-item email is ever sent),
  and when the configured timezone cannot be loaded.
- Comments across the codebase trimmed to a concise one-line style; no
  behavioral changes.

## [3.1] — 2026-06-29

Hardening release for the **Sleuth** AI assistant, following an independent
audit. `init_db()` only adds new tables and columns; it never modifies existing
rows, so a production database is left intact.

### Sleuth AI assistant

- **Project-scoped answers.** The cloud assistant's free-form data path and
  retrieval are now scoped to the projects the requesting user can access.
  Managers and regular users can no longer read bugs, statistics, or reports
  from projects they aren't assigned to. The write firewall is unchanged; the
  cloud layer still cannot perform any write.
- **Non-repetitive replies.** Conversational answers use mild sampling
  (temperature plus frequency/presence penalties). Routing, the LLM judge, the
  read-only agent, and ingestion remain fully deterministic. Greetings, thanks,
  and help requests resolve from rules without reaching the model.
- **New guardrails.** Added a prompt-injection/instruction-extraction boundary,
  a flag for hallucinated write claims ("I closed / assigned ..."), an app-side
  answer-length ceiling, control-character scrubbing, a fenced-and-defanged
  judge prompt, and a canonical-query length cap.
- **Evaluation harness.** An offline, network-free harness covering six
  standards: LLM-as-judge grounding, agent trajectory, outcome, confidence
  calibration (Brier score), reliability (answer variance/route stability), and
  pass@k. Backed by in-process observability counters (provider, route, judge
  outcomes, cooldown trips).

## [3.0] — 2026-06-13

A self-hosted tracker for bugs, requirements, and tasks, built on FastAPI +
PostgreSQL with a React + TypeScript frontend. `init_db()` creates new tables
and columns on boot without touching existing rows, so a production database is
left intact.

### Work items

- Bugs, requirements, and tasks share one `#N` counter. A tab strip filters
  KPIs, columns, and analytics by type. Each type has its own statuses; bugs
  also carry a DEV/UAT/PROD environment.
- Items can be linked (relates, blocks, duplicate). Adding or removing a link
  requires edit rights on both items and is safe under concurrent requests.
- Rich-text descriptions and comments (bold, italic, lists, code, quotes). PDF,
  image, and video files are stored in PostgreSQL. Pasted images become real
  attachments; image metadata (EXIF) is stripped.
- Bulk status, priority, environment, and delete actions across multiple items,
  each row checked for concurrent edits.

### Projects, events, and reporting

- Projects group work; events group items for a standup or sprint and have one
  or more managers.
- Managers and admins can export a multi-sheet Excel report, with a row limit
  per export.
- In-app notification bell with a live unread badge, email (per event or a
  daily digest), and optional browser push via Firebase Cloud Messaging. The
  push table is keyed on the token plus a platform column, so a native Android
  client uses the same send path.
- Audit log of every create, update, delete, and login, readable by admins and
  managers. Entries persist after an item is deleted.
- Admins can see every active session (user, role, IP, browser, time) and log
  out a single device.

### Login and roles

- Local login with bcrypt-hashed passwords and three roles: admin, manager, and
  user. Role checks apply the same way on the REST and chat write paths.
- Server-side sessions, signed HttpOnly SameSite cookies (Secure over HTTPS),
  per-account lockout, evened-out login timing to prevent email enumeration, and
  an optional HaveIBeenPwned check on password set.
- Password reset that doesn't reveal whether an email exists, using single-use,
  hashed, expiring tokens.

### Sleuth assistant

An in-app assistant that answers plain-English questions and runs audited
actions. It tries four layers in order and is fully local by default:

1. A rule-based parser over verbs, filters, names, and IDs.
2. A TF-IDF / cosine-similarity classifier over a curated corpus, with no
   external model files.
3. An optional, lazily loaded local LLM (llama.cpp against a GGUF model).
4. An optional cloud LLM (Groq or OpenRouter), off by default.

Read intents only `SELECT`. Write intents go through the same audited paths as
the REST API, are confirmed before any change, and are re-checked against the
write policy at confirm time. When the cloud layer is on, all outbound text
passes through a secret-redaction filter first. Four read-only accuracy add-ons
(grounding, a multi-step agent, citation verification, and answer evaluation)
can each be turned on separately.

### Security

- Content-Security-Policy (`script-src 'self'`, no CDN) and a full set of
  security headers, applied to error responses (`429`, `403`, `500`) too.
- CSRF Origin/Referer checks on state-changing requests, including login.
- Per-IP and per-account rate limits on login, password reset,
  change-password, commenting, and chat.
- Rich text is sanitized server-side against an allowlist, then again in the
  browser with DOMPurify before display.
- Guards against mass-assignment on item-type changes, length caps on request
  lists and search terms, a global request body-size cap, and formula-injection
  defense on Excel export.
- Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are served only
  outside production.

### Reliability and performance

- Bug and bulk edits use optimistic concurrency: a stale save returns `409`
  instead of overwriting another user's edit.
- If the database is down at boot, the app serves a degraded state
  (`/api/health` reports `status: degraded`) instead of crash-looping.
- Oversized or decompression-bomb images are skipped by a pixel budget and fall
  back to the original bytes.
- The frontend code-splits secondary views, the assistant panel, and the
  rich-text editor to load them on demand. Idle data and session polls skip
  re-rendering when nothing changed. Reports and list endpoints are row-limited
  server-side; indexes keep audit-trail and digest queries fast.

### Notes

- The frontend is built with Vite into `app/static`, which FastAPI serves
  directly.
- Secrets (`.env`, `secrets/firebase-admin.json`) are gitignored and set up per
  server.
