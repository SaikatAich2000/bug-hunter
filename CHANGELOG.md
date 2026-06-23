# Changelog

All notable changes to Bug Hunter. The format follows
[Keep a Changelog](https://keepachangelog.com/).

## [3.0] — 2026-06-13

A self-hosted tracker for bugs, requirements, and tasks, built on FastAPI +
PostgreSQL with a React + TypeScript frontend. Upgrades are additive: `init_db()`
creates any new tables and columns on boot and never changes existing rows, so an
existing production database is left intact.

### Work items

- Bugs, requirements, and tasks share one `#N` counter. A tab strip filters the
  KPIs, columns, and analytics to the type you pick. Each type has its own
  statuses; bugs also carry a DEV/UAT/PROD environment.
- Link items together (relates, blocks, duplicate). Adding or removing a link
  needs edit rights on both items and is safe under concurrent requests.
- Rich-text descriptions and comments (bold, italic, lists, code, quotes). PDF,
  image, and video files are stored in PostgreSQL. Pasted images become real
  attachments, and image metadata (EXIF) is stripped.
- Bulk status, priority, environment, and delete actions across many items at
  once, each row checked for concurrent edits.

### Projects, events, and reporting

- Projects group your work; events group items for a standup or sprint and have
  one or more managers.
- A report builder (manager and admin) that exports a multi-sheet Excel file,
  with a row limit per export.
- In-app notification bell with a live unread badge, email (per event or one
  daily digest), and optional browser push via Firebase Cloud Messaging. The
  push table is keyed on the token plus a platform column, so a native Android
  client uses the same send path.
- An audit log of every create, update, delete, and login, readable by admins
  and managers. Entries stay even after an item is deleted.
- Admins see every active session (user, role, IP, browser, time) and can log
  out a single device.

### Login and roles

- Local login with bcrypt-hashed passwords and three roles: admin, manager, and
  user. Role checks are enforced the same way on the REST and chat write paths.
- Server-side sessions, signed HttpOnly SameSite cookies (Secure over HTTPS),
  per-account lockout, evened-out login timing so emails can't be enumerated, and
  an optional HaveIBeenPwned check when setting a password.
- Password reset that doesn't reveal whether an email exists, with single-use,
  hashed, expiring tokens.

### Sleuth assistant

An in-app assistant that answers plain-English questions and runs audited
actions. It tries four layers in order and is fully local by default:

1. A rule-based parser over verbs, filters, names, and IDs.
2. A TF-IDF / cosine-similarity classifier over a curated corpus, with no
   external model files.
3. An optional, lazily loaded local LLM (llama.cpp against a GGUF model).
4. An optional cloud LLM (Gemini or OpenRouter), off by default.

Read intents only `SELECT`. Write intents go through the same audited paths as
the REST API, are confirmed before any change, and are re-checked against the
write policy at confirm time. When the cloud layer is on, all outbound text
passes through a secret-redaction filter first, and four read-only accuracy
add-ons (grounding, a multi-step agent, citation verification, and answer
evaluation) can each be turned on separately.

### Security

- Content-Security-Policy (`script-src 'self'`, no CDN) and a full set of
  security headers, applied to error responses (`429`, `403`, `500`) too.
- CSRF Origin/Referer checks on state-changing requests, including login.
- Per-IP and per-account rate limits on login, password reset,
  change-password, commenting, and chat.
- Rich text cleaned server-side against an allowlist, then again in the browser
  with DOMPurify before it's shown.
- A guard against mass-assignment on item-type changes, length caps on request
  lists and search terms, a global request body-size cap, and spreadsheet
  formula-injection defense on Excel export.
- Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) served only outside
  production.

### Reliability and performance

- Bug and bulk edits use optimistic concurrency: a stale save returns `409`
  instead of overwriting someone else's edit.
- If the database is down at boot, the app degrades to a serving state
  (`/api/health` reports `status: degraded`) instead of crash-looping.
- Huge or decompression-bomb images are skipped by a pixel budget and fall back
  to the original bytes.
- The frontend code-splits secondary views, the assistant panel, and the
  rich-text editor so they load on demand. Idle data and session polls skip
  re-rendering when nothing changed. Reports and list endpoints are row-limited
  server-side, and indexes keep the audit-trail and digest queries fast.

### Notes

- The frontend is built with Vite into `app/static`, which FastAPI serves
  directly.
- Secrets (`.env`, `secrets/firebase-admin.json`) are gitignored and set up per
  server.
