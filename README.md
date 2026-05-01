# 🐞 Bug Hunter

A self-hosted, internal-use issue tracker. Built with FastAPI + PostgreSQL + a
zero-framework JavaScript SPA. One Docker command to run, no external auth, no
external file storage — attachments live in the database itself.

## Features

- **Login + role-based access** — admin, manager, user; bcrypt password hashing
- **Bug tracking** with status, priority, environment (DEV / UAT / PROD)
- **Multi-assignee** support — many users per bug
- **Comments and attachments** (PDF, image, video) stored as BLOBs in Postgres
- **Email notifications** on bug create / update / assignment / new comment (Gmail / Outlook / SMTP)
- **Forgot-password** flow via email reset link
- **Full audit trail** — every create / update / delete / login logged and viewable
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
docker compose up -d --build
```

Open **http://localhost:8765** in your browser.

That's it. Postgres runs in its own isolated Docker container on port `55432`
(intentionally non-standard so it won't collide with anything you have on
`5432`). The app listens on port `8765`.

### First login

On first run, Bug Hunter auto-creates an admin user from the `BOOTSTRAP_ADMIN_*`
env vars. Defaults:

- email: `admin@bughunter.local`
- password: `ChangeMe123!`

Log in, then **immediately** change the password from the Account panel in the
sidebar. After that, only admins can create new accounts (User Management
section, sidebar). Roles:

| Role    | What they can do                                                |
|---------|-----------------------------------------------------------------|
| admin   | Everything — including creating, editing, deleting users        |
| manager | Edit any bug, manage projects; cannot manage users              |
| user    | Edit only their own bugs (where they're reporter or assignee)   |

### Production checklist

Before exposing this to a real network, set these in `.env`:

```bash
SESSION_SECRET=$(openssl rand -hex 32)   # generate a long random secret
COOKIE_SECURE=true                        # only if serving over HTTPS
BOOTSTRAP_ADMIN_EMAIL=you@yourcompany.com
BOOTSTRAP_ADMIN_PASSWORD=<a strong password>
APP_BASE_URL=https://bugs.yourcompany.com
```

Then `docker compose down && docker compose up -d` to apply.

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
4. Restart: `docker compose down && docker compose up -d --build`

Other providers (Office 365, Mailtrap, SendGrid, etc.) work the same way —
just point at their SMTP host and credentials.

## Stopping

```bash
docker compose down            # stop containers
docker compose down -v         # stop AND wipe the database
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
│   ├── config.py        # env-driven settings
│   ├── database.py      # SQLAlchemy setup
│   ├── email_service.py # SMTP / console email backends
│   ├── main.py          # FastAPI entry point
│   ├── models.py        # User, Project, Bug, Comment, Attachment, Activity
│   ├── routes/          # users, projects, bugs, stats, audit
│   ├── schemas.py       # Pydantic DTOs
│   └── static/          # index.html + app.js + styles.css + favicon.svg
├── tests/               # pytest end-to-end tests
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

## Running tests

```bash
pip install -r requirements.txt
pytest
```

## Contributing

Issues and pull requests welcome. Please run the tests before submitting.

## License

Released under the [MIT License](LICENSE). See the LICENSE file for details.
