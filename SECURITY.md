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

The following controls are already in place. See the 3.0 entry (including the
*Post-release hardening* subsection) in [CHANGELOG.md](CHANGELOG.md) for detail.

- **Authentication** — bcrypt password hashing; signed, HttpOnly, SameSite
  session cookies (`Secure` under HTTPS); server-side session records that
  admins can revoke per device.
- **Account protection** — per-account lockout after repeated failures, login
  timing equalized against account enumeration, and an optional HaveIBeenPwned
  breach check on every set-password path.
- **Password reset** — account-enumeration-safe by default, with single-use,
  hashed, expiring tokens.
- **CSRF** — a defense-in-depth Origin/Referer check on state-changing requests,
  in addition to SameSite cookies.
- **Rate limiting** — in-memory per-IP and per-account limits on auth-sensitive
  endpoints (login, reset, change-password, commenting, chat).
- **HTTP headers** — Content-Security-Policy (`script-src 'self'`, no CDN),
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Permissions-Policy`, cross-origin isolation headers, and HSTS under HTTPS.
- **Request limits** — a global body-size cap and row-bounded list and report
  endpoints.
- **Authorization** — role-based access enforced identically on the REST and
  chat write paths, plus a mass-assignment guard on item-type changes.
- **Uploads** — EXIF/metadata stripping on uploaded images and content-type
  defenses on attachments.
- **Data export** — spreadsheet formula-injection defense on the XLSX writer.
- **Assistant egress** — the optional cloud LLM is off by default; when enabled,
  all outbound text is passed through a secret-redaction filter, and Sleuth never
  writes data through the model.
- **Audit trail** — every create, update, delete, and login is logged and
  survives item deletion.

See also the *Live-data safety* and *Production checklist* sections of
[README.md](README.md).
