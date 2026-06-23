# Security Policy

## Supported versions

Only the latest minor release gets security fixes.

| Version | Supported |
|---------|-----------|
| 3.0.x   | Yes       |
| < 3.0   | No        |

## Reporting a vulnerability

Please don't open a public GitHub issue for security problems.

Instead, open a private **GitHub Security Advisory** on this repository (the
*Security* tab → *Report a vulnerability*). This keeps the report private until a
fix ships.

Please include:

- The affected version (`GET /api/health` returns it, or see `app/__init__.py`).
- Steps to reproduce — a minimal proof of concept is fine.
- What the impact is.
- A suggested fix, if you have one.

## Response

- We acknowledge within 5 working days.
- We assess severity within 10 working days.
- Patches are best-effort; we prefer coordinated disclosure.
- We credit you on request once the fix is public.

## Security posture

These protections are built in. See the 3.0 entry in
[CHANGELOG.md](CHANGELOG.md) for more detail.

- **Login** — passwords are hashed with bcrypt. Session cookies are signed,
  HttpOnly, and SameSite (`Secure` over HTTPS). Sessions are stored server-side,
  so admins can log out any device.
- **Account protection** — accounts lock after repeated failed logins, login
  timing is evened out so attackers can't tell which emails exist, and an
  optional HaveIBeenPwned check runs whenever a password is set.
- **Password reset** — reset doesn't reveal whether an email exists, and tokens
  are single-use, hashed, and expire.
- **Email** — outbound headers are stripped of line breaks to block header
  injection, and the console backend never logs message bodies (including live
  reset links).
- **CSRF** — state-changing requests get an extra Origin/Referer check on top of
  the SameSite cookies.
- **Rate limiting** — per-IP and per-account limits on sensitive endpoints
  (login, reset, change-password, commenting, chat).
- **HTTP headers** — Content-Security-Policy (`script-src 'self'`, no CDN), plus
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Permissions-Policy`, cross-origin isolation headers, and HSTS over HTTPS. The
  same headers apply to error responses (`429`, `403`, and a generic `500`).
- **API surface** — interactive API docs (`/docs`, `/redoc`, `/openapi.json`)
  are served only in development; a production deploy (`COOKIE_SECURE`) turns
  them off.
- **Request limits** — a global cap on request body size, and row limits on list
  and report endpoints.
- **Authorization** — role checks are enforced the same way on the REST and chat
  write paths, plus a guard against mass-assignment when an item's type changes.
- **Concurrent edits** — bug updates use optimistic concurrency, so a stale save
  returns `409` instead of silently overwriting someone else's change.
- **Uploads** — image metadata (EXIF) is stripped on upload (huge or
  decompression-bomb images are skipped by a pixel budget), plus content-type
  checks on attachments.
- **Output** — stored rich text is cleaned server-side against an allowlist, and
  again in the browser with DOMPurify before it's shown.
- **Data export** — the Excel writer defends against spreadsheet formula
  injection.
- **Assistant egress** — the optional cloud LLM is off by default. When it's on,
  all outbound text passes through a secret-redaction filter first. Sleuth never
  writes data through the model: even the optional multi-step agent uses only
  read-only tools that re-check through the same write firewall as the REST API.
- **Audit trail** — every create, update, delete, and login is logged, and the
  log survives item deletion.

See also the *Live-data safety* and *Production checklist* sections of
[README.md](README.md).
