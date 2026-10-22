<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Troubleshooting

Fixes for common problems.

> [← Back to the README](../README.md)

## Troubleshooting

- **`APP_VERSION is required`:** add `APP_VERSION=<release>` to `.env`.
- **App refuses to start in production:** the log lists every unmet requirement (session secret, bootstrap password, auto-login, Git encryption key, and with `APP_ENV=production` also https URL, secure cookies and email backend).
- **Database connection refused:** check `docker compose ps`; the app waits for the database health check, so rerun `./deploy.sh` once it's healthy.
- **Port 8765 in use:** stop the other service or change the host port in `docker-compose.yml`.
- **Logged out after every restart:** set a fixed `SESSION_SECRET`.
- **Password rejected as too weak:** the default policy is at least `PASSWORD_MIN_LENGTH` (8) characters containing at least one letter and one number (`PASSWORD_REQUIRE_COMPLEXITY`); a few very common passwords are refused outright.
- **Emails not sent:** check `EMAIL_BACKEND=smtp` and the `SMTP_*` values, and set `LOG_LEVEL=DEBUG` for SMTP details.
- **Blank page or 404 on assets:** rebuild the frontend (`cd frontend && npm run build`) and restart the backend so it recalculates the asset version.
- **401s right after login:** `COOKIE_SECURE=true` needs https; use `false` for plain http.
- **Sprints shows "Scrum isn't set up":** Agile is off for that project. A manager or admin clicks **Set up Scrum board** on the Sprints page.
- **Logs:** `docker compose logs -f app`.
