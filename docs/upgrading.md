<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Live-data safety and deployment

How data stays safe, how to upgrade, and how the stack is deployed.

> [← Back to the README](../README.md)

## Live-data safety

- `./deploy.sh` rebuilds the image and restarts containers without touching the `bugtracker_pgdata` volume.
- `./down.sh` (no flags) stops containers and keeps the volume.
- Data is lost only through explicit commands: `./down.sh --wipe-db` (asks for confirmation) or `docker compose down -v`.
- `init_db()` only creates missing tables, columns and indexes. Existing rows are never deleted, and the only in-place writes are one-time backfills of new columns (for example display IDs and default workflow statuses).
- Because the schema is additive, an older release still runs against a newer database. Rolling back is a manual step (below), so take a backup before upgrading. A drill of backup, delete, restore and rollback to the previous `APP_VERSION` kept the data.

**Backup & restore:** `./deploy.sh` takes a dump first (`SKIP_BACKUP=1` skips it).
Manually:

```bash
./scripts/backup.sh                       # backups/bughunter-<UTC time>.dump, keeps the newest 14
./scripts/restore.sh backups/<file>.dump  # asks to confirm, takes a safety dump, stops the app, restores
```

Copy dumps off the host. Details in [docs/operations.md](operations.md).

**Rollback:** `./down.sh`, check out the previous tag (or `docker load` a saved
image and set `APP_VERSION` to it), then `./deploy.sh`. To also revert data, use `restore.sh`.

## Deployment

To upgrade production: take a database backup, `git pull`, set `APP_VERSION`
in `.env` to the new release, and run `./deploy.sh`. There's no separate
migration step: `init_db()` adds missing tables, columns and indexes on boot and
never drops or rewrites existing rows (see *Live-data safety*), apart from
the one-time Sprints upgrade described under *Features*. `./down.sh`
keeps all data; only `./down.sh --wipe-db` deletes it. To roll back, `./down.sh`,
check out the previous tag and `./deploy.sh` again (an older app still runs
against a newer schema).

| Aspect | How it's handled |
| --- | --- |
| Services | The app container and a PostgreSQL container |
| Ports | App `8765 → 8000`; PostgreSQL only on `127.0.0.1:55432` |
| Resource limits | Compose caps the stack at 0.5 vCPU / 512 MB (app 0.30 / 320 MB, database 0.20 / 192 MB). Measured results and how to reproduce them: [docs/performance.md](performance.md). |
| Workers | One Uvicorn worker and a small SQLAlchemy pool (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`, 2 + 2 in Compose). The digest scheduler and rate limiters are per process, so scale out only after moving them out of the app. |
| Secrets | Injected from `.env` / `secrets/` by Compose, or by the platform; never baked into the image |
| Health | `curl -fsS http://localhost:8765/api/health` |

The app container runs only the app. It doesn't run Docker or Compose and
doesn't mount the Docker socket. Without Compose (for example on Azure Container
Apps), the platform must inject a `DATABASE_URL` that points at an external
PostgreSQL, never `localhost`.
