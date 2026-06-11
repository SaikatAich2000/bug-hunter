# Security policy

## Supported versions

Only the latest minor release receives security fixes.

| Version | Supported |
|---------|-----------|
| 2.10.x  | ✅        |
| < 2.10  | ❌        |

## Reporting a vulnerability

**Do not open a public GitHub issue for security findings.**

Open a private **GitHub Security Advisory** on this repository
(*Security* tab → *Report a vulnerability*). That keeps the report
confidential until a fix ships.

Please include:

- Affected version (`GET /api/health` returns it, or check
  `app/__init__.py`).
- Reproduction steps — minimal proof of concept is fine.
- Impact assessment.
- Suggested fix (optional).

## Response

- Acknowledgement within **5 working days**.
- Triaged severity within **10 working days**.
- Patches on best-effort timeline; coordinated disclosure preferred.
- Credit on request once the fix is public.

## Existing security posture

See the **v2.10** entry in [CHANGELOG.md](CHANGELOG.md) and the
*Live-data safety* and *Production checklist* sections of
[README.md](README.md) for what's already in place — cookie auth with
HttpOnly + SameSite + signed token, CSRF middleware, rate limiting,
account lockout, HaveIBeenPwned breach check, EXIF strip on uploads,
attachment content-type defenses, bcrypt password hashing, and a full
audit trail.
