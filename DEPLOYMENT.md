# Deploying Bug Hunter

Bug Hunter runs as two Docker containers: the **app** and a **PostgreSQL database**. Data lives in a separate Docker volume, decoupled from the app. Deploy with one script. Update the same way.

> **Quick start**
> 1. Fill in a `.env` file, then run `./deploy.sh`.
> 2. To update: `git pull` then `./deploy.sh`.
> 3. Upgrades only add new tables/columns — existing data is never modified.

---

## Requirements

- A Linux server (or VM) with **Docker** and **Docker Compose v2**.

No Node, Python, or build tools needed on the server. The frontend ships pre-built.

---

## 1. First-time install

```bash
git clone <your-repo-url> bug-hunter
cd bug-hunter
cp .env.example .env
```

Open `.env` and set at least these values:

| Setting | What to put |
|---|---|
| `POSTGRES_PASSWORD` | A strong database password. |
| `SESSION_SECRET` | A long random string. Run `openssl rand -hex 32`. Required for https. |
| `APP_BASE_URL` | The URL users visit, e.g. `https://bugs.example.com`. |
| `COOKIE_SECURE` | `true` for https, `false` otherwise. |
| `BOOTSTRAP_ADMIN_PASSWORD` | The first admin's password. Change it after first login. |

> ⚠️ **`POSTGRES_PASSWORD` is set once.** PostgreSQL writes it into the data volume on first start. Changing the value later does not update the live database password.

Then deploy:

```bash
./deploy.sh
```

When you see **"Bug Hunter deployed successfully!"**, open `http://your-server:8765` (or your domain) and log in. **Change the admin password immediately.**

> The app listens on host port **8765**. Postgres binds to **127.0.0.1:55432** (local only). All data is stored in the Docker volume **`bugtracker_pgdata`**.

---

## 2. Optional features: AI assistant & push notifications

Both are off by default. Add the relevant settings to `.env` and re-run `./deploy.sh`. Never commit secrets to git — place them on the server directly (see [Copying files to the server](#copying-files-to-the-server)).

### Sleuth AI assistant (Groq)

Add to `.env`:

```bash
SLEUTH_CLOUD_ENABLED=1
GROQ_API_KEY=your-key-from-console.groq.com
GROQ_MODEL=llama-3.3-70b-versatile
```

### Push notifications (Firebase)

1. Place your Firebase service-account file on the server at `secrets/firebase-admin.json`. The app reads it automatically — no path needed in `.env`.
2. Add to `.env`:

```bash
WEB_PUSH_ENABLED=true
FIREBASE_API_KEY=...
FIREBASE_AUTH_DOMAIN=your-project.firebaseapp.com
FIREBASE_PROJECT_ID=...
FIREBASE_MESSAGING_SENDER_ID=...
FIREBASE_APP_ID=...
FIREBASE_VAPID_KEY=...
```

The six `FIREBASE_*` values come from Firebase console → **Project settings** → your **Web app**. The `firebase-admin.json` file is a separate server-side key; both are required.

After editing `.env`, re-run `./deploy.sh`.

### Copying files to the server

Secrets and `.env` stay on the server only — never in git.

- **WinSCP (Windows):** connect via SFTP (port `22`). Drag `firebase-admin.json` into `bug-hunter/secrets/` (create the folder if needed). To edit `.env`, double-click it in WinSCP's editor and save with `Ctrl+S`.
- **scp (command line):**
  ```bash
  ssh youruser@your-server "mkdir -p ~/bug-hunter/secrets"
  scp firebase-admin.json youruser@your-server:~/bug-hunter/secrets/
  ```

---

## 3. Updating to a new version

```bash
cd ~/bug-hunter
git pull
./deploy.sh
```

The frontend is pre-built and ships with the code, so nothing is built on the server. `git pull` does not touch `.env`, `secrets/`, or the database.

> Take a backup first (see below) — optional, but quick.

---

## 4. How upgrades handle the database

- Data lives in the Docker volume **`bugtracker_pgdata`**, separate from the app container. Upgrading the app never empties or modifies it.
- Schema changes are **additive only**: on start the app creates missing tables and adds missing columns with safe defaults. It never drops, renames, or alters existing data.
- Both `./deploy.sh` and `./down.sh` preserve all data.
- ⚠️ The only command that destroys data is `./down.sh --wipe-db`. It requires you to type `YES` to confirm.

### Backup & restore

```bash
# Back up the database to a file
docker exec -t bugtracker_db pg_dump -U bugtracker bugtracker > backup.sql

# Restore from that file
cat backup.sql | docker exec -i bugtracker_db psql -U bugtracker bugtracker
```

If you changed the database name or user in `.env`, substitute those values for `bugtracker`.

---

## 5. Rolling back

1. `./down.sh` — stops the app, keeps all data.
2. Switch to the previous code (`git checkout <old-tag>`), or load a saved image with `docker load -i <image>.tar.gz` and update the `app:` image tag in `docker-compose.yml`.
3. `./deploy.sh`.

Because upgrades are additive, an older app version still runs against a newer database schema.

---

## 6. After upgrading from an older version: project access

Version 3.1 restricts managers and regular users to only the projects they are assigned to. Right after upgrading, they will see an empty screen until an admin assigns them — this is expected.

To fix: log in as **admin** → **Users** → open each person → tick their **Projects** → **Save**. Admins always see all projects.

---

## 7. For developers (building the frontend)

The server serves the pre-built files in `app/static`. Before pushing changes:

```bash
cd frontend
npm install      # first time only
npm run build    # writes the bundle into ../app/static
```

Commit everything **including the updated `app/static`**, then push. Secrets (`.env`, `secrets/`) are gitignored, so `git add -A` is safe.

---

## Quick reference

| Task | Command |
|---|---|
| Deploy / update | `./deploy.sh` |
| Stop (keep data) | `./down.sh` |
| Back up the database | `docker exec -t bugtracker_db pg_dump -U bugtracker bugtracker > backup.sql` |
| View logs | `docker compose logs -f` |
| Health check | `curl http://localhost:8765/api/health` |

---

## Troubleshooting

- **App won't start over https:** set `SESSION_SECRET` to a value of at least 32 characters. Generate one with `openssl rand -hex 32`.
- **Can't reach the site:** run `docker ps` and confirm both `bugtracker_app` and `bugtracker_db` are up. Check that port `8765` is open in your firewall.
- **Database "unhealthy" at boot:** the app starts in degraded mode and `GET /api/health` reports the database as unavailable until it recovers. Check logs with `docker compose logs -f db`.
- **Digest emails missing or arriving at the wrong time:** the digest replaces immediate emails, so both `EMAIL_DIGEST_ENABLED=true` and `EMAIL_DIGEST_CRON` must be set, and the container must be **rebuilt** (`./deploy.sh`, not a plain restart) so the timezone data installs. Confirm the startup log shows `Email-digest scheduler started (cron=..., tz=Asia/Kolkata)` — `tz=UTC` or `falling back to UTC` means the rebuild didn't take. Test immediately with `docker exec bugtracker_app python -m app.jobs.email_digest`. Keep `EMAIL_DIGEST_LOOKBACK_HOURS` at least twice the gap between runs (daily = 50).
