#!/usr/bin/env bash
# Build the schema on a PostgreSQL database that has the pgtap extension, then run tests/db/schema.sql.
#   scripts/qa/run_pgtap.sh postgresql+psycopg://postgres:<password>@127.0.0.1:5432/<empty database>
# Prints TAP lines; exits non-zero when any assertion fails.
set -euo pipefail
cd "$(dirname "$0")/../.."
URL="${1:?usage: run_pgtap.sh <sqlalchemy database url>}"
PY=".venv/bin/python"; [ -x "$PY" ] || PY=".venv/Scripts/python.exe"
DATABASE_URL="$URL" "$PY" -c "from app.database import init_db; init_db()"
PSQL_URL="${URL/postgresql+psycopg/postgresql}"
psql "$PSQL_URL" -qAt -c "CREATE EXTENSION IF NOT EXISTS pgtap"
OUT=$(psql "$PSQL_URL" -qAt -f tests/db/schema.sql)
echo "$OUT" | grep -E "^(not ok|# )" || true
echo "$OUT" | grep -c "^ok" | sed 's/^/passed: /'
! echo "$OUT" | grep -q "^not ok"
