#!/usr/bin/env bash
# Nightly backup: pg_dump (custom format) + restic offsite of parquet manifests.
# Raw JSONL is intentionally excluded from offsite (too big; reproducible).
set -euo pipefail

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${PMRE_BACKUP_DIR:-/data/backups}"
mkdir -p "${BACKUP_DIR}"

# DATABASE_URL like postgresql+psycopg2://user:pass@host:port/db
DB_URL="${PMRE_DATABASE_URL:?PMRE_DATABASE_URL must be set}"
python - "$DB_URL" "${BACKUP_DIR}/pmre_${STAMP}.dump" <<'PY'
import sys
from pmre.ops.backup import pg_dump_command
print(" ".join(pg_dump_command(sys.argv[1], sys.argv[2])))
PY

DUMP_CMD="$(python - "$DB_URL" "${BACKUP_DIR}/pmre_${STAMP}.dump" <<'PY'
import sys
from pmre.ops.backup import pg_dump_command
print(" ".join(pg_dump_command(sys.argv[1], sys.argv[2])))
PY
)"
echo "running: ${DUMP_CMD}"
PGPASSWORD="${PMRE_DB_PASSWORD:-}" ${DUMP_CMD}

# Offsite parquet manifests (requires RESTIC_REPOSITORY + RESTIC_PASSWORD env).
if command -v restic >/dev/null 2>&1 && [ -n "${RESTIC_REPOSITORY:-}" ]; then
    restic backup "${PMRE_PARQUET_DIR:-/data/parquet}" "${BACKUP_DIR}/pmre_${STAMP}.dump"
fi

# Retain 14 days locally.
find "${BACKUP_DIR}" -name 'pmre_*.dump' -mtime +14 -delete
echo "backup complete: ${BACKUP_DIR}/pmre_${STAMP}.dump"
