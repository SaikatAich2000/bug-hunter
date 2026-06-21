# Security Policy

## Supported versions

Only the latest minor release receives security fixes.

| Version | Supported |
|---------|-----------|
| 3.0.x   | Yes       |
| < 3.0   | No        |

## Reporting a vulnerability

Do not open a public GitHub issue for security findings.

Open a private **GitHub Security Advisory** on this repository (the *Security*
tab → *Report a vulnerability*). This keeps the report confidential until a fix
ships.

Please include:

- The affected version (`GET /api/health` returns it, or see `app/__init__.py`).
- Reproduction steps; a minimal proof of concept is fine.
- An impact assessment.
- A suggested fix, if you have one.

## Response

- Acknowledgement within 5 working days.
- Triaged severity within 10 working days.
- Patches on a best-effort timeline; coordinated disclosure preferred.
- Credit on request once the fix is public.

## Security posture

The following controls are in place. See the 3.0 entry in
[CHANGELOG.md](CHANGELOG.md) for detail.

- **Authentication** — bcrypt password hashing; signed, HttpOnly, SameSite
  session cookies (`Secure` under HTTPS); server-side session records that
  admins can revoke per device.
- **Account protection** — per-account lockout after repeated failures, login
  timing equalized against account enumeration, and an optional HaveIBeenPwned
  breach check on every set-password path.
- **Password reset** — account-enumeration-safe by default, with single-use,
  hashed, expiring tokens.
- **Email** — outbound message headers are CR/LF-stripped against header
  injection, and the console backend never logs message bodies (including live
  reset links).
- **CSRF** — a defense-in-depth Origin/Referer check on state-changing requests,
  in addition to SameSite cookies.
- **Rate limiting** — in-memory per-IP and per-account limits on auth-sensitive
  endpoints (login, reset, change-password, commenting, chat).
- **HTTP headers** — Content-Security-Policy (`script-src 'self'`, no CDN),
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Permissions-Policy`, cross-origin isolation headers, and HSTS under HTTPS. The
  rate-limit `429`, CSRF `403`, and a generic, no-detail `500` carry the same
  header set.
- **API surface** — interactive API docs (`/docs`, `/redoc`, `/openapi.json`)
  are served only in development; a production deploy (`COOKIE_SECURE`) disables
  them.
- **Request limits** — a global body-size cap and row-bounded list and report
  endpoints.
- **Authorization** — role-based access enforced identically on the REST and
  chat write paths, plus a mass-assignment guard on item-type changes.
- **Concurrent edits** — bug updates use optimistic concurrency, so a stale save
  returns `409` rather than silently overwriting another user's change.
- **Uploads** — EXIF/metadata stripping on uploaded images (oversized or
  decompression-bomb rasters are skipped by a pixel budget) and content-type
  defenses on attachments.
- **Output handling** — stored rich text is sanitized server-side against an
  element/attribute allowlist and again client-side with DOMPurify before render.
- **Data export** — spreadsheet formula-injection defense on the XLSX writer.
- **Assistant egress** — the optional cloud LLM is off by default; when enabled,
  all outbound text is passed through a secret-redaction filter. Sleuth never
  writes data through the model: even the optional multi-step agent uses only
  read-only tools that re-parse through the same write firewall as the REST API.
- **Audit trail** — every create, update, delete, and login is logged and
  survives item deletion.

See also the *Live-data safety* and *Production checklist* sections of
[README.md](README.md).
