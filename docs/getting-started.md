<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Getting started

Run Bug Hunter with Docker in a few minutes.

> [← Back to the README](../README.md)

## Quick start

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Docker Engine + Compose v2).

```bash
git clone https://github.com/<your-org>/bug-hunter.git
cd bug-hunter
cp .env.example .env       # set APP_VERSION, POSTGRES_PASSWORD, BOOTSTRAP_ADMIN_* at minimum
./deploy.sh
```

`./deploy.sh` builds the image (including the frontend), starts PostgreSQL,
waits for it to be healthy, then starts the app. Open <http://localhost:8765>.

PostgreSQL runs in its own container, published only on `127.0.0.1:55432` so it
doesn't clash with a local PostgreSQL install. Data lives in the named volume
`bugtracker_pgdata`, which `./deploy.sh` and `./down.sh` never delete.

For a clean rebuild, run `BUILD_CLEAN=1 ./deploy.sh`. Behind a corporate proxy or
air gap, set `BASE_IMAGE` in `.env` to an internal mirror, or pre-load
`python:3.12-slim` and `node:22-slim` with `docker save | docker load`.

### First login

On an empty database, Bug Hunter creates a first admin from
`BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` (Compose defaults the email
to `admin@bughunter.local`; the password has no default and must be set). Change
the password right away from the profile menu. A production deploy refuses to
start while the `.env.example` placeholder password is still set.

Set `AUTO_LOGIN_ENABLED=true` to skip the login screen entirely: every visitor
is signed in automatically as the bootstrap admin. Use it only on a trusted
local deployment. Production refuses to start with it on.

### Production checklist

```bash
APP_VERSION=<release tag>
SESSION_SECRET=$(openssl rand -hex 32)    # at least 32 characters, never the placeholder
COOKIE_SECURE=true                        # only when serving over HTTPS
BOOTSTRAP_ADMIN_EMAIL=you@example.com
BOOTSTRAP_ADMIN_PASSWORD=<a strong password>
APP_BASE_URL=https://bugs.example.com
CORS_ORIGINS=https://bugs.example.com
APP_ENV=production                        # strict checks: https URL, secure cookies, real email backend
```

Then `./down.sh && ./deploy.sh`. If a requirement is unmet, the app logs every
problem at once and refuses to start.

### Stopping

```bash
./down.sh                  # stop containers; keep database volume and image
./down.sh --wipe-db        # also delete the database volume (asks for confirmation)
./down.sh --remove-images  # also remove the built image
./down.sh --full-clean     # both
```
