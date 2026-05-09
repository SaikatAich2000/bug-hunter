# 🐞 Bug Hunter

A self-hosted, internal-use issue tracker. Built with FastAPI + PostgreSQL + a
zero-framework JavaScript SPA. One Docker command to run, no external auth, no
external file storage — attachments live in the database itself.

## Features

- **Login + role-based access** — admin, manager, user; bcrypt password hashing
- **Per-session tracking & admin revocation** (Keycloak-style) — admins can
  see every active session across the system and log a specific device out
  without affecting any other session for the same user
- **Bug tracking** with status, priority, environment (DEV / UAT / PROD)
- **Multi-assignee** support — many users per bug
- **Single-screen Jira-style bug detail** — title, description, metadata,
  comments and attachments are all on one wide screen; no separate edit modal,
  no pencil button to chase
- **Comments and attachments** (PDF, image, video) stored as BLOBs in Postgres
- **Email notifications** on bug create / update / assignment / new comment
  (Gmail / Outlook / SMTP)
- **Forgot-password** flow via email reset link
- **Full audit trail** — every create / update / delete / login logged and
  viewable by admins and managers
- **Light / dark themes**, fully responsive (mobile, tablet, desktop)
- **CSV export** of all bugs

## Quick start

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running

### Run it

```bash
git clone https://github.com/YOUR_USERNAME/bug-hunter.git
cd bug-hunter
cp .env.example .env       # edit if you want email enabled — see below
./deploy.sh
```

Open **http://localhost:8765** in your browser.

That's it. Postgres runs in its own isolated Docker container on port `55432`
(intentionally non-standard so it won't collide with anything you have on
`5432`). The app listens on port `8765`. The named volume `bugtracker_pgdata`
holds your live data and is **never** removed by `./deploy.sh` or by a plain
`./down.sh` — see "Live-data safety" below.

### First login

On first run, Bug Hunter auto-creates an admin user from the `BOOTSTRAP_ADMIN_*`
env vars. Defaults:

- email: `admin@bughunter.local`
- password: `ChangeMe123!`

Log in, then **immediately** change the password from the Account panel in the
sidebar. After that, admins (and managers, with limits) can create new
accounts. Roles:

| Role    | Bugs                                          | Projects                  | Users                                                              | Audit | Sessions        |
|---------|-----------------------------------------------|---------------------------|--------------------------------------------------------------------|-------|-----------------|
| admin   | Create, edit any, **delete any**, reassign    | Create, edit, **delete**  | Create, edit, **delete**                                           | ✓     | ✓ list + revoke |
| manager | Create, edit any, reassign (no delete)        | Create, edit (no delete)  | Create, edit non-admins (no delete, no admin role grant)           | ✓     | —               |
| user    | Create, edit any, reassign (no delete)        | View only                 | View only                                                          | —     | —               |

Notes on the v3.1 policy:

- **Bug deletion is admin-only.** Even the user who reported a bug can't
  delete it; only admins can. Managers used to be allowed but no longer.
- **Project / user deletion is admin-only.** Managers create and edit, but
  delete is reserved.
- **Managers can't grant the admin role**, can't edit existing admins, and
  can't deactivate them.
- **Audit Trail is hidden from regular users** — they don't see who did what
  across the system.
- **Admins can revoke any session.** Listed under the "Sessions" sidebar
  item: shows user, role, IP, browser, when it started, when it was last
  seen, when it expires. The admin's own current session is flagged "This
  is you" and can't be revoked from the panel — use Log out for that.

### Production checklist

Before exposing this to a real network, set these in `.env`:

```bash
SESSION_SECRET=$(openssl rand -hex 32)   # generate a long random secret
COOKIE_SECURE=true                        # only if serving over HTTPS
BOOTSTRAP_ADMIN_EMAIL=you@yourcompany.com
BOOTSTRAP_ADMIN_PASSWORD=<a strong password>
APP_BASE_URL=https://bugs.yourcompany.com
CORS_ORIGINS=https://bugs.yourcompany.com
```

Then `./down.sh && ./deploy.sh` to apply.

## Configuring email (optional)

By default `EMAIL_BACKEND=console`, which just logs emails to the app log
instead of sending them — perfect for trying things out.

To send real notifications via Gmail:

1. Enable **2-Step Verification** on your Google account.
2. Generate an [App Password](https://myaccount.google.com/apppasswords) (16 characters).
3. Edit your `.env`:
   ```env
   EMAIL_BACKEND=smtp
   EMAIL_FROM=Bug Hunter <you@gmail.com>
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USERNAME=you@gmail.com
   SMTP_PASSWORD=xxxx xxxx xxxx xxxx
   SMTP_USE_TLS=true
   ```
4. Restart: `./down.sh && ./deploy.sh`

Other providers (Office 365, Mailtrap, SendGrid, etc.) work the same way —
just point at their SMTP host and credentials.

## Live-data safety

`./deploy.sh` rebuilds the application image and restarts the stack. It does
**not** touch the `bugtracker_pgdata` volume that holds your Postgres data.
`./down.sh` (no flags) stops the containers and leaves the volume intact.
The only ways to lose data are:

- `./down.sh --wipe-db` — explicitly asks you to type `YES` first
- `docker compose down -v` — manual destructive call
- Manually deleting the named volume

Schema changes in v3.1 are **purely additive**:

- A new `sessions` table is created on first start (idempotent, only if
  it doesn't already exist). No existing table's columns or indexes change.
- Cookies issued by older builds (which don't carry a `jti`) are still
  accepted and treated as legacy sessions, so a redeploy doesn't kick
  every user out at once.

## Stopping

```bash
./down.sh                  # stop containers, KEEP database volume + image
./down.sh --wipe-db        # also wipe the database (asks for YES)
./down.sh --remove-images  # also remove the built image
./down.sh --full-clean     # both
```

## Tech stack

- **Backend:** FastAPI 0.115, SQLAlchemy 2.0, Pydantic 2, psycopg 3
- **Database:** PostgreSQL 16
- **Frontend:** Vanilla JavaScript (no framework), CSS variables for theming
- **Container:** Python 3.12 slim image, multi-service Docker Compose

## Project structure

```
.
├── app/
│   ├── config.py          # env-driven settings
│   ├── database.py        # SQLAlchemy setup
│   ├── email_service.py   # SMTP / console email backends
│   ├── main.py            # FastAPI entry point
│   ├── models.py          # User, Project, Bug, Comment, Attachment,
│   │                      # Activity, PasswordResetToken, Session
│   ├── routes/            # auth, users, projects, bugs, stats, audit, sessions
│   ├── schemas.py         # Pydantic DTOs
│   └── static/            # index.html + login.html + reset.html
│                          # + app.js + styles.css + favicons
├── tests/                 # pytest end-to-end tests (225 passing)
├── docker-compose.yml
├── Dockerfile
├── deploy.sh              # build + start (idempotent, safe on re-run)
├── down.sh                # stop (data-safe by default)
├── requirements.txt
└── .env.example           # copy to .env and edit
```

## Running tests

```bash
pip install -r requirements.txt
pytest tests/
```

## Contributing

Issues and pull requests welcome. Please run the tests before submitting.

## License

Released under the [MIT License](LICENSE.txt). See the LICENSE file for details.
