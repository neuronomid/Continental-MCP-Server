#!/usr/bin/env bash
#
# run.sh — start the entire pm-research-engine locally.
#
# Usage:
#   ./run.sh [mode]
#
# Modes:
#   all         (default) migrate + calendar + collectors + REST + MCP + hourly analytics loop
#   serve       migrate + calendar + REST + MCP only (no collectors)
#   demo        seed a synthetic dataset + run analytics, then serve REST + MCP (fully offline)
#   collectors  just the collectors (btc_feed, clob_ws, discovery, snapshotter, resolution)
#
# Env:
#   Values come from ./.env if present, else sensible dev defaults are used.
#   SKIP_SYNC=1   skip `uv sync`
#
# Stop everything with Ctrl-C — all child processes are shut down cleanly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
MODE="${1:-all}"

# ---- pretty logging -------------------------------------------------------
_c(){ printf '\033[%sm' "$1"; }
log(){  printf '%s[run]%s %s\n' "$(_c '1;36')" "$(_c 0)" "$*"; }
warn(){ printf '%s[run]%s %s\n' "$(_c '1;33')" "$(_c 0)" "$*" >&2; }
err(){  printf '%s[run]%s %s\n' "$(_c '1;31')" "$(_c 0)" "$*" >&2; }

command -v uv >/dev/null 2>&1 || { err "uv not found — install from https://docs.astral.sh/uv/"; exit 1; }

# ---- environment ----------------------------------------------------------
if [[ -f .env ]]; then
  log "loading .env"
  set -a; # shellcheck disable=SC1091
  source .env; set +a
else
  warn "no .env found — using dev defaults (copy .env.example to customize)"
fi

# A multi-process launch needs a shared DB that supports CONCURRENT WRITERS.
# SQLite serialises writers behind a single write lock, so the collectors
# deadlock with "database is locked". The local run therefore uses PostgreSQL +
# TimescaleDB (auto-started as a Docker container below unless the operator
# overrides PMRE_DATABASE_URL to point at their own instance).
export PMRE_ENV="${PMRE_ENV:-dev}"
export PMRE_DATA_DIR="${PMRE_DATA_DIR:-$SCRIPT_DIR/data}"
export PMRE_PARQUET_DIR="${PMRE_PARQUET_DIR:-$PMRE_DATA_DIR/parquet}"
export PMRE_RAW_DIR="${PMRE_RAW_DIR:-$PMRE_DATA_DIR/raw}"
export PMRE_DATABASE_URL="${PMRE_DATABASE_URL:-postgresql+psycopg2://pmre:pmre@127.0.0.1:55432/pmre}"
export PMRE_PG_CONTAINER="${PMRE_PG_CONTAINER:-pmre-tsdb}"
export PMRE_PG_HOST_PORT="${PMRE_PG_HOST_PORT:-55432}"
export PMRE_SERVING_HOST="${PMRE_SERVING_HOST:-127.0.0.1}"
# Host port 8080 is occupied by the WireGuard/wstunnel container on this box,
# so REST defaults to 8081 and 8080 is refused outright (never bind it).
export PMRE_REST_PORT="${PMRE_REST_PORT:-8081}"
export PMRE_MCP_PORT="${PMRE_MCP_PORT:-8090}"
if [[ "$PMRE_REST_PORT" == "8080" ]]; then
  warn "REST port 8080 is reserved on this host — using 8081 instead"
  export PMRE_REST_PORT=8081
fi
# Dev auth defaults so the endpoints are actually callable out of the box.
export PMRE_REST_BEARER_TOKENS="${PMRE_REST_BEARER_TOKENS:-dev-read-token}"
export PMRE_MCP_BEARER_TOKEN="${PMRE_MCP_BEARER_TOKEN:-dev-mcp-token}"
export PMRE_INGEST_BEARER_TOKEN="${PMRE_INGEST_BEARER_TOKEN:-dev-ingest-token}"

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$PMRE_DATA_DIR" "$PMRE_PARQUET_DIR" "$PMRE_RAW_DIR" "$LOG_DIR"

READ_TOKEN="${PMRE_REST_BEARER_TOKENS%%,*}"   # first read token

# ---- dependencies ---------------------------------------------------------
if [[ "${SKIP_SYNC:-0}" != "1" ]]; then
  log "syncing dependencies (uv sync) — set SKIP_SYNC=1 to skip"
  uv sync --quiet
fi

# ---- ensure a concurrent-writer database is reachable --------------------
pg_ready(){ # 0 when the configured DB accepts a trivial authenticated query
  uv run python - <<'PY' >/dev/null 2>&1
import os, sys
from sqlalchemy import create_engine, text
try:
    create_engine(os.environ["PMRE_DATABASE_URL"]).connect().execute(text("select 1"))
except Exception:
    sys.exit(1)
PY
}

ensure_postgres(){
  case "$PMRE_DATABASE_URL" in postgresql*|postgres*) ;; *) return 0 ;; esac
  if pg_ready; then log "database reachable ✓"; return 0; fi
  command -v docker >/dev/null 2>&1 || {
    err "Postgres unreachable and docker not installed — start it or set PMRE_DATABASE_URL"; exit 1; }
  if docker ps -a --format '{{.Names}}' | grep -qx "$PMRE_PG_CONTAINER"; then
    log "starting existing '$PMRE_PG_CONTAINER' container"
    docker start "$PMRE_PG_CONTAINER" >/dev/null
  else
    log "launching TimescaleDB container '$PMRE_PG_CONTAINER' on :$PMRE_PG_HOST_PORT"
    docker run -d --name "$PMRE_PG_CONTAINER" \
      -e POSTGRES_USER=pmre -e POSTGRES_PASSWORD=pmre -e POSTGRES_DB=pmre \
      -p "${PMRE_PG_HOST_PORT}:5432" timescale/timescaledb:latest-pg16 >/dev/null
  fi
  log "waiting for database to accept connections…"
  for _ in $(seq 1 90); do pg_ready && { log "database is up ✓"; return 0; }; sleep 1; done
  err "database did not become ready"; exit 1
}
ensure_postgres

# ---- prerequisites (idempotent) ------------------------------------------
log "applying database migrations → $PMRE_DATABASE_URL"
uv run python -m pmre migrate

log "materializing session calendar (90 days)"
uv run python -m pmre materialize-calendar --days 90 || warn "calendar step failed (continuing)"

log "ensuring a dev ingest token exists"
uv run python - <<'PY' || warn "ingest-token setup skipped"
from sqlalchemy import select
from pmre.config import load_settings
from pmre.db.engine import Database
from pmre.db.models import IngestToken
from pmre.serving.auth import create_ingest_token, hash_token

s = load_settings()
db = Database(s.database_url)
tok = s.ingest_bearer_token or "dev-ingest-token"
with db.session_factory() as ses:
    exists = ses.execute(select(IngestToken).where(IngestToken.token_hash == hash_token(tok))).scalar_one_or_none()
if not exists:
    create_ingest_token(db.session_factory, token=tok, label="run.sh")
    print("created ingest token")
else:
    print("ingest token already present")
PY

# ---- process supervision --------------------------------------------------
declare -a PIDS=() NAMES=()

start(){ # start <name> <cmd...>
  local name="$1"; shift
  log "starting ${name} (logs/${name}.log)"
  ( exec "$@" ) >"$LOG_DIR/${name}.log" 2>&1 &
  PIDS+=("$!"); NAMES+=("$name")
}

cleanup(){
  trap - INT TERM EXIT
  echo
  log "shutting down ${#PIDS[@]} service(s)…"
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  log "all services stopped."
}
trap cleanup INT TERM EXIT

analytics_loop(){
  # Run the hourly analytics job every hour (daily/weekly would be systemd timers in prod).
  while true; do
    sleep 3600
    uv run python -m pmre analytics-hourly || true
  done
}

# ---- mode dispatch --------------------------------------------------------
COLLECTORS=(btc_feed clob_ws discovery snapshotter resolution)

case "$MODE" in
  demo)
    log "seeding synthetic dataset + running full analytics pipeline (offline)…"
    uv run python -m pmre pipeline-demo --days 25
    start rest uv run python -m pmre serve-rest
    start mcp  uv run python -m pmre serve-mcp
    ;;
  serve)
    start rest uv run python -m pmre serve-rest
    start mcp  uv run python -m pmre serve-mcp
    ;;
  collectors)
    for c in "${COLLECTORS[@]}"; do start "collector-$c" uv run python -m pmre collector "$c"; done
    ;;
  all)
    for c in "${COLLECTORS[@]}"; do start "collector-$c" uv run python -m pmre collector "$c"; done
    start rest uv run python -m pmre serve-rest
    start mcp  uv run python -m pmre serve-mcp
    start analytics bash -c "$(declare -f analytics_loop); analytics_loop"
    ;;
  -h|--help|help)
    sed -n '2,20p' "$0"; exit 0
    ;;
  *)
    err "unknown mode '$MODE' (use: all | serve | demo | collectors)"; exit 1
    ;;
esac

# ---- wait for REST health, then print a summary ---------------------------
REST_URL="http://${PMRE_SERVING_HOST}:${PMRE_REST_PORT}"
MCP_URL="http://${PMRE_SERVING_HOST}:${PMRE_MCP_PORT}/mcp"
if [[ "$MODE" != "collectors" ]] && command -v curl >/dev/null 2>&1; then
  log "waiting for REST health…"
  for _ in $(seq 1 30); do
    if curl -fsS "${REST_URL}/v1/health" >/dev/null 2>&1; then log "REST is up ✓"; break; fi
    sleep 1
  done
fi

cat <<EOF

$(_c '1;32')pm-research-engine is running$(_c 0)  (mode: ${MODE})
  DB            ${PMRE_DATABASE_URL}
  REST          ${REST_URL}        (Bearer ${READ_TOKEN})
  MCP           ${MCP_URL}     (Bearer ${PMRE_MCP_BEARER_TOKEN})
  ingest token  ${PMRE_INGEST_BEARER_TOKEN}
  logs          ${LOG_DIR}/

  try:  curl -s ${REST_URL}/v1/health | head -c 400 ; echo
        curl -s -H "Authorization: Bearer ${READ_TOKEN}" "${REST_URL}/v1/candidates" | head -c 400 ; echo
        tail -f ${LOG_DIR}/rest.log

  Press Ctrl-C to stop everything.
EOF

# Block until interrupted (a crashing child won't tear the whole launcher down).
set +e
wait
