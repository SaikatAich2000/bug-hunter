# Security Policy

## Supported versions

Only the latest minor release receives security fixes.

| Version | Supported |
|---------|-----------|
| 3.1.x   | Yes       |
| < 3.1   | No        |

## Reporting a vulnerability

Do not open a public GitHub issue for security problems.

Open a private **GitHub Security Advisory** instead (*Security* tab → *Report a vulnerability*). This keeps the report confidential until a fix ships.

Include:

- The affected version (`GET /api/health` returns it, or see `app/__init__.py`).
- Steps to reproduce — a minimal proof of concept is fine.
- Impact.
- A suggested fix, if you have one.

## Response

- We acknowledge within 5 working days.
- We assess severity within 10 working days.
- Patches are best-effort; we prefer coordinated disclosure.
- We credit you on request once the fix is public.

## Security posture

See [CHANGELOG.md](CHANGELOG.md) for more detail.

- **Login** — passwords are hashed with bcrypt. Session cookies are signed, HttpOnly, and SameSite (`Secure` over HTTPS). Sessions are stored server-side so admins can revoke any device.
- **Account protection** — accounts lock after repeated failed logins. Login timing is normalized so attackers cannot enumerate emails. An optional HaveIBeenPwned check runs whenever a password is set.
- **Password reset** — reset does not reveal whether an email exists. Tokens are single-use, hashed, and expire.
- **Email** — outbound headers are stripped of line breaks to block header injection. The console backend never logs message bodies (including live reset links).
- **CSRF** — state-changing requests get an Origin/Referer check on top of SameSite cookies.
- **Rate limiting** — per-IP and per-account limits on sensitive endpoints (login, reset, change-password, commenting, chat).
- **HTTP headers** — Content-Security-Policy (`script-src 'self'`, no CDN), plus `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, cross-origin isolation headers, and HSTS over HTTPS. The same headers apply to error responses (`429`, `403`, and a generic `500`).
- **API surface** — interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are served only in development. A production deploy (`COOKIE_SECURE`) disables them.
- **Request limits** — a global cap on request body size, and row limits on list and report endpoints.
- **Authorization** — role checks are enforced the same way on both the REST and chat write paths, with a guard against mass-assignment when an item's type changes.
- **Concurrent edits** — bug updates use optimistic concurrency. A stale save returns `409` instead of silently overwriting another user's change.
- **Uploads** — EXIF metadata is stripped on upload. Oversized or decompression-bomb images are rejected by a pixel budget. Content-type checks apply to all attachments.
- **Output** — stored rich text is sanitized server-side against an allowlist, then again in the browser with DOMPurify before rendering.
- **Data export** — the Excel writer defends against spreadsheet formula injection.
- **Assistant egress** — the optional cloud LLM is off by default. When enabled, all outbound text passes through a secret-redaction filter first. Sleuth never writes data through the model: even the multi-step agent uses only read-only tools, which re-check through the same write firewall as the REST API.
- **Audit trail** — every create, update, delete, and login is logged. The log survives item deletion.

See also the *Live-data safety* and *Production checklist* sections of [README.md](README.md).
