# Deploying Bug Hunter

A friendly, step-by-step guide to running Bug Hunter on a server — and updating
it later — **without ever putting your data at risk**.

Bug Hunter runs as two Docker containers: the **app** and a **PostgreSQL
database**. Your data lives in a separate, sealed Docker volume, so the app and
the data are always kept apart. Deploying is one script. Updating is the same
script again.

> **The 30-second version**
> 1. Put settings in a `.env` file, then run `./deploy.sh`.
> 2. To update later: `git pull` then `./deploy.sh`.
> 3. Your database is never touched — upgrades only *add* new tables/columns,
>    never delete or change existing data.

---

## What you need

- A Linux server (or VM) with **Docker** and **Docker Compose v2**.

That's it. You do **not** need Node, Python, or any build tools on the server —
the website is shipped pre-built inside the code.

---

## 1. First-time install

```bash
git clone <your-repo-url> bug-hunter
cd bug-hunter
cp .env.example .env
```

Open `.env` and set at least these:

| Setting | What to put |
|---|---|
| `POSTGRES_PASSWORD` | A strong database password. |
| `SESSION_SECRET` | A long random string — run `openssl rand -hex 32`. **Required** if you serve over https. |
| `APP_BASE_URL` | The address people visit, e.g. `https://bugs.example.com`. |
| `COOKIE_SECURE` | `true` if you serve over https, otherwise `false`. |
| `BOOTSTRAP_ADMIN_PASSWORD` | The first admin's password (change it from the default). |

> ⚠️ **`POSTGRES_PASSWORD` is set once.** PostgreSQL bakes it in the first time
> the (empty) data volume starts. Changing it later does **not** change the live
> database password — so pick it now and keep it.

Then deploy:

```bash
./deploy.sh
```

Wait for the green **"Bug Hunter deployed successfully!"** message, open
`http://your-server:8765` (or your domain), and log in with the bootstrap admin.
**Change the admin password right after the first login.**

> The stack runs isolated: app on host port **8765**, Postgres on
> **127.0.0.1:55432** (local only), and all data in a Docker volume named
> **`bugtracker_pgdata`**.

---

## 2. Optional features: AI assistant & push notifications

Both are **off by default**. The app works perfectly without them — turn them on
whenever you like by adding settings to `.env` (and, for push, placing one
secret file on the server). **Secrets must never be committed to git** — you put
them on the server directly (see [Copying files to the server](#copying-files-to-the-server)).

### Sleuth AI assistant (Gemini)

Add to `.env`:

```bash
SLEUTH_CLOUD_ENABLED=1
GEMINI_API_KEY=your-key-from-google-ai-studio
GEMINI_MODEL=gemini-2.5-flash
```

### Push notifications (Firebase)

1. Place your Firebase **service-account** file on the server at
   `secrets/firebase-admin.json`. The app reads it from there automatically (you
   do **not** add a path to `.env`).
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

The six `FIREBASE_*` values come from your Firebase console → **Project
settings** → your **Web app**. (The `firebase-admin.json` file is a separate,
server-side key — you need both for push to work end to end.)

After editing `.env`, re-run `./deploy.sh`.

### Copying files to the server

Secrets and `.env` live **only** on the server. Two easy ways to put them there:

- **WinSCP (Windows, drag-and-drop):** connect over **SFTP** (host = your
  server, port `22`, your username and password). Drag `firebase-admin.json`
  into the `bug-hunter/secrets/` folder (create the folder if it isn't there).
  You can also double-click `.env` to edit it in WinSCP's built-in editor —
  `Ctrl+S` saves and uploads automatically.
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

That's the whole update. The frontend is pre-built and travels with the code, so
there's nothing to build on the server. Your `.env`, `secrets/` folder, and
database are **left untouched** by `git pull`.

> 💡 Take a quick backup first (below) — optional, but it costs nothing.

---

## 4. Your database is safe (how upgrades work)

- Your data lives in the Docker volume **`bugtracker_pgdata`**, separate from the
  app. Upgrading the app never opens or empties it.
- Schema changes are **strictly additive**: on start, the app creates any
  missing tables and adds any missing columns (with safe default values). It
  **never drops, renames, or alters** existing data.
- Both `./deploy.sh` and a plain `./down.sh` keep all your data.
- ⚠️ The **only** command that erases data is `./down.sh --wipe-db` — and it
  forces you to type `YES` first. Don't run it unless you truly mean to.

### Backup & restore

```bash
# Back up the whole database to a file
docker exec -t bugtracker_db pg_dump -U bugtracker bugtracker > backup.sql

# Restore it later, if ever needed
cat backup.sql | docker exec -i bugtracker_db psql -U bugtracker bugtracker
```

(If you changed the database name/user in `.env`, use those instead of
`bugtracker`.)

---

## 5. Rolling back

1. `./down.sh` — stops the app, **keeps your data**.
2. Switch back to the previous code (`git checkout <old-tag>`), or load a saved
   image with `docker load -i <image>.tar.gz` and point `docker-compose.yml`'s
   `app:` service at that image tag.
3. `./deploy.sh`.

Because upgrades are additive, an older version still runs fine against the
newer database — nothing was removed.

---

## 6. After upgrading from an older version: project access

Version 3.0 limits managers and regular users to **only the projects they're
assigned to**. Right after upgrading, they'll see an **empty screen** until an
admin assigns them — this is expected.

Fix it in ~2 minutes: log in as **admin** → **Users** → open each person → tick
their **Projects** → **Save**. Admins always see everything, so you're never
locked out.

---

## 7. For developers (building the frontend)

The server runs the **pre-built** files in `app/static`; it does not build
anything. Before pushing code changes:

```bash
cd frontend
npm install      # first time only
npm run build    # writes the bundle into ../app/static
```

Commit everything **including the updated `app/static`**, then push. Secrets
(`.env`, `secrets/`) are gitignored and never leave your machine, so
`git add -A` is safe.

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

- **App won't start over https:** make sure `SESSION_SECRET` is set to a long
  value (≥ 32 characters). Generate one with `openssl rand -hex 32`.
- **Can't reach the site:** check the containers are up with `docker ps` (look
  for `bugtracker_app` and `bugtracker_db`), and that port `8765` is open.
- **Database "unhealthy" at boot:** the app starts in a degraded mode and
  `GET /api/health` reports the database as unavailable until it recovers —
  check `docker compose logs -f db`.
