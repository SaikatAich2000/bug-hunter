# Changelog

All notable changes to Bug Hunter. The format follows
[Keep a Changelog](https://keepachangelog.com/).

## [3.0] — 2026-06-13

Self-hosted work-item tracker built on FastAPI and PostgreSQL with a React +
TypeScript single-page frontend. Schema changes are strictly additive: new
tables and columns are created by `init_db()` on boot, and existing rows are
never modified, so an existing production database is left intact on upgrade.

### Work items

- Bugs, requirements, and tasks share one `#N` counter. A tab strip scopes
  KPIs, filters, columns, and analytics to the active type. Each type has its
  own status set; bugs additionally carry a DEV/UAT/PROD environment.
- Directed links between items: relates, blocks, duplicate. Adding or removing
  a link requires edit rights on both endpoints and is idempotent under
  concurrent requests.
- Rich-text descriptions and comments (bold, italic, lists, code, blockquote).
  PDF, image, and video attachments are stored as PostgreSQL blobs. Pasted
  images upload as real attachments, and uploaded image metadata (EXIF) is
  stripped.
- Bulk status, priority, environment, and delete actions across many selected
  items in one request, with per-row optimistic concurrency.

### Projects, events, and reporting

- Projects group work; events group items for a standup or sprint, each with
  one or more managers.
- Report builder (manager and admin) with multi-sheet XLSX export, row-bounded
  per export.
- Per-user in-app notification bell with a live unread badge, per-operation or
  daily-digest email, and optional browser push via Firebase Cloud Messaging.
  The push table is keyed on the registration token plus a platform column, so
  a native Android client registers into the same send path.
- Audit log of every create, update, delete, and login, readable by admins and
  managers; entries survive item deletion.
- Admins list every active session (user, role, IP, browser, timestamps) and
  can revoke a single device.

### Authentication and authorization

- Local login with bcrypt-hashed passwords and three roles: admin, manager,
  user. Role-based access is enforced identically on the REST and chat write
  paths.
- Server-side session records, signed HttpOnly SameSite cookies (Secure under
  HTTPS), per-account lockout, login timing equalized against account
  enumeration, and an optional HaveIBeenPwned breach check on set-password.
- Account-enumeration-safe password reset with single-use, hashed, expiring
  tokens.

### Sleuth assistant

In-app assistant that answers natural-language questions and runs audited
actions. It escalates through four layers and is fully local by default:

1. A rule-based parser over verbs, filters, names, and IDs.
2. A TF-IDF and cosine-similarity classifier over a curated corpus, with no
   external model files.
3. An optional, lazily loaded local LLM (llama.cpp against a GGUF model).
4. An optional cloud LLM (Gemini or OpenRouter), off by default.

Read intents only `SELECT`; write intents go through the same audited paths as
the REST API, are confirmed before any change, and are re-checked against the
write policy at confirm time. When the cloud layer is enabled, all outbound
text passes through a secret-redaction filter first, and four read-only
accuracy layers (grounding, a multi-step agent, citation verification, and
answer evaluation) can each be enabled independently.

### Security

- Content-Security-Policy (`script-src 'self'`, no CDN) and a full security
  header set, applied to error responses (`429`, `403`, `500`) as well.
- CSRF Origin/Referer checks on state-changing requests, including login.
- In-memory per-IP and per-account rate limits on login, password reset,
  change-password, commenting, and chat.
- Server-side rich-text sanitization against an element/attribute allowlist,
  re-applied client-side with DOMPurify before render.
- Mass-assignment guard on item-type changes, length caps on request lists and
  search terms, a global request body-size cap, and spreadsheet
  formula-injection defense on XLSX export.
- Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are served only
  outside production.

### Reliability and performance

- Bug and bulk edits use optimistic concurrency: a stale save returns `409`
  rather than overwriting a concurrent edit.
- A database outage at boot degrades to a serving state (`/api/health` reports
  `status: degraded`) instead of crash-looping.
- Oversized or decompression-bomb images are skipped by a pixel budget and fail
  open to the original bytes.
- Frontend code-splitting loads secondary views, the assistant panel, and the
  rich-text editor on demand. Idle data and session polls skip re-render when
  nothing changed. Reports and list endpoints are row-bounded server-side, and
  covering indexes keep the audit trail and digest queries sargable.

### Notes

- The frontend is built with Vite into `app/static`, which FastAPI serves
  directly.
- Secrets (`.env`, `secrets/firebase-admin.json`) are gitignored and
  provisioned per host.
