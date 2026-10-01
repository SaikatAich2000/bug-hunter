# Deploying Bug Hunter

Bug Hunter runs as two Docker containers: the **app** and a **PostgreSQL database**. Data lives in a separate Docker volume, decoupled from the app. Deploy with one script. Update the same way.

> **Quick start**
> 1. Fill in a `.env` file, then run `./deploy.sh`.
> 2. To update: `git pull` then `./deploy.sh`.
> 3. Upgrades only add new tables/columns — existing data is never modified.

---

## Requirements

- A Linux server (or VM) with **Docker** and **Docker Compose v2**.

No Node or Python needed on the server: the image build compiles the frontend in a
throwaway `node:20-slim` stage, so the build host needs access to the npm registry
(or a mirror) as well as to Docker Hub.

---

## 1. First-time install

```bash
git clone <your-repo-url> bug-hunter
cd bug-hunter
cp .env.example .env
```

If the server has Python 3, `python3 scripts/gen_local_env_secrets.py` fills
`POSTGRES_PASSWORD`, `SESSION_SECRET`, `BOOTSTRAP_ADMIN_PASSWORD` and
`GIT_CREDENTIAL_ENCRYPTION_KEY` with strong random values for you. It needs no
extra packages, and it never overwrites a value that's already real.

Open `.env` and set at least these values:

| Setting | What to put |
|---|---|
| `APP_VERSION` | The release you are deploying, e.g. the tag you checked out. Required — the image is tagged with it. |
| `POSTGRES_PASSWORD` | A strong database password. Required. |
| `SESSION_SECRET` | A long random string. Run `openssl rand -hex 32`. Required for https. |
| `APP_BASE_URL` | The URL users visit, e.g. `https://bugs.example.com`. |
| `COOKIE_SECURE` | `true` for https, `false` otherwise. |
| `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` | The first admin's login. Both required on a fresh database; change the password after first login. |
| `APP_ENV` | Leave `development` for a trial. Set `production` for the strict start-up checks (https URL, secure cookies, real email backend). |

> ⚠️ **`POSTGRES_PASSWORD` is set once.** PostgreSQL writes it into the data volume on first start. Changing the value later does not update the live database password.

Then deploy:

```bash
./deploy.sh
```

When you see **"Bug Hunter deployed successfully!"**, open `http://your-server:8765` (or your domain) and log in. **Change the admin password immediately.**

> The app listens on host port **8765**. Postgres binds to **127.0.0.1:55432** (local only). All data is stored in the Docker volume **`bugtracker_pgdata`**.

---

## 2. Optional features: AI assistant, push notifications, Git, observability

All of these are off by default. Add the relevant settings to `.env` and re-run `./deploy.sh`. Never commit secrets to git — place them on the server directly (see [Copying files to the server](#copying-files-to-the-server)).

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

### GitHub feature branches (User Stories)

```bash
GIT_BRANCH_CREATION_ENABLED=true
GIT_BRANCH_DELETION_ENABLED=false        # removal is a separate opt-in
GIT_CREDENTIAL_ENCRYPTION_KEY=...        # Fernet key; needed to store per-project tokens
```

Generate the key once with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
and keep it in your secret store — losing or rotating it makes saved project tokens
unreadable. Then, per project, open **Projects → Edit → Git integration**, enter the
API URL, organization and a fine-grained token, and use **Test connection**. Behind a
TLS-intercepting proxy, mount the corporate CA bundle and set `GIT_CA_BUNDLE_FILE`.

### Observability (OpenTelemetry / SigNoz)

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to your OTLP/gRPC collector (for example
`http://otel-collector:4317`). Traces, metrics and logs then stream there alongside
the console; leave it blank to stay console-only.

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

`./deploy.sh` rebuilds the image, including the frontend. `git pull` does not touch `.env`, `secrets/`, or the database. Bump `APP_VERSION` in `.env` to the release you pulled.

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

## 7. Upgrading from 3.x to 4.0

- **Add `APP_VERSION`** to `.env` (for example `APP_VERSION=4.0`). Docker Compose now refuses to start without it.
- **`POSTGRES_PASSWORD` must be set** in `.env`. Older templates already set `bugtracker_pw`; keep whatever value your volume was created with.
- **New tables and columns** for sprints, releases, labels, Git branches and display IDs are added automatically on first boot. Existing items keep their numbers and data.
- **Agile is opt-in per project.** Nothing changes until a manager or admin opens **Sprints**, picks a project and clicks **Set up Scrum board**.
- **API docs** stay off in production unless `ENABLE_API_DOCS=true`, as before.
- **Item descriptions become plain text.** Descriptions saved with formatting by 3.x display as plain text, and are stored as plain text the next time the item is saved. Comments keep rich text. Take a backup first if you want to keep the original formatting.
- **First boot builds one new index** (`idx_bugs_project_updated_id`); on a large database that first start takes a little longer.
- **Tested path:** a 3.1 database populated through the 3.1 API was upgraded on PostgreSQL 16 with every item, comment, attachment (byte-identical), link, event and login preserved, and a second boot changed nothing (`audit/TESTING_REPORT.md`).

## 8. For developers (building the frontend)

The server serves the pre-built files in `app/static`. Before pushing changes:

```bash
cd frontend
npm ci           # first time only
npm test         # Vitest behavioural tests
npm run build    # writes the bundle into ../app/static
```

Commit everything **including the updated `app/static`**, then push. The Docker image
rebuilds the bundle anyway, but the committed copy keeps `python -m uvicorn` runs current. Secrets (`.env`, `secrets/`) are gitignored, so `git add -A` is safe.

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

- **App won't start over https:** set `SESSION_SECRET` to a value of at least 32 characters (not the `.env.example` placeholder). Generate one with `openssl rand -hex 32`. With `APP_ENV=production` the log lists every other unmet requirement.
- **`APP_VERSION is required`:** add `APP_VERSION=<release>` to `.env`.
- **Can't reach the site:** run `docker ps` and confirm both `bugtracker_app` and `bugtracker_db` are up. Check that port `8765` is open in your firewall.
- **Database "unhealthy" at boot:** the app starts in degraded mode and `GET /api/health` reports the database as unavailable until it recovers. Check logs with `docker compose logs -f db`.
- **Digest emails missing or arriving at the wrong time:** the digest replaces immediate emails, so both `EMAIL_DIGEST_ENABLED=true` and `EMAIL_DIGEST_CRON` must be set, and the container must be **rebuilt** (`./deploy.sh`, not a plain restart) so the timezone data installs. Confirm the startup log shows `Email-digest scheduler started (cron=..., tz=Asia/Kolkata)` — `tz=UTC` or `falling back to UTC` means the rebuild didn't take. Test immediately with `docker exec bugtracker_app python -m app.jobs.email_digest`. Keep `EMAIL_DIGEST_LOOKBACK_HOURS` at least twice the gap between runs (daily = 50).
