#!/usr/bin/env bash
# Bring up the matrix test environment.
#
# Container runtime: Podman (rootless or rootful — both work). The
# compose file is the standard compose-spec format and is consumed by
# `podman compose`. If you have Docker installed instead, the same
# `docker compose <subcmd>` commands work against the same file —
# substitute manually.
#
# Steps:
#   1. podman compose up -d, wait for postgres healthcheck
#   2. drop+recreate the matrix_e2e database with the pgvector extension
#   3. render tests/.e2e/config.yaml
#   4. launch `uv run matrix api --run-worker` in the background
#   5. poll /v1/health until 200 (30 s timeout)
#
# Exit code 0 means the server is up and ready for tests.
# Exit code 1 means something failed — relevant diagnostics are dumped to
# stderr before exiting, and the caller (the test loop) should NOT try to
# run tests; it should mark the iteration BLOCKED and call teardown.sh.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

# Windows-friendly PATH augmentation: when this script is invoked from
# pytest / a Claude Code Bash tool / a bare git-bash window, $PATH may
# not include the Windows app dirs where podman lives. Prepend the
# common locations idempotently so the script can find it. Mirrors
# scripts/e2e/ui-bringup.sh's shim.
case "${OS:-}${OSTYPE:-}" in
    *win*|*Win*|*msys*|*cygwin*)
        for p in \
            "/c/Users/${USERNAME:-$USER}/AppData/Local/Programs/Podman" \
            "/c/Users/${USERNAME:-$USER}/AppData/Local/Microsoft/WindowsApps" \
            "/c/Program Files/podman" ; do
            if [[ -d "$p" && ":$PATH:" != *":$p:"* ]]; then
                PATH="$p:$PATH"
            fi
        done
        export PATH
        ;;
esac

E2E_DIR="$ROOT/tests/.e2e"
LOG_DIR="$E2E_DIR/logs"
CONFIG="$E2E_DIR/config.yaml"
PID_FILE="$E2E_DIR/server.pid"
STDOUT_FILE="$E2E_DIR/server.stdout"

PORT="${MATRIX_E2E_PORT:-8765}"
DB_USER="${MATRIX_DB_USER:-matrix}"
DB_PASSWORD="${MATRIX_DB_PASSWORD:-matrix}"
DB_NAME="matrix_e2e"
# Set MATRIX_E2E_NO_VECTOR=1 to render an AppConfig without a vector_store
# block. Used by gating tests that need to assert 503 behaviour on the
# collection / document / search routes when the subsystem is disabled.
NO_VECTOR="${MATRIX_E2E_NO_VECTOR:-0}"

mkdir -p "$E2E_DIR" "$LOG_DIR"

# ---- 1. Docker --------------------------------------------------------------

echo "[bringup] starting postgres container..." >&2
podman compose up -d postgres >&2

echo "[bringup] waiting for postgres healthcheck..." >&2
deadline=$(( $(date +%s) + 60 ))
until podman compose exec -T postgres pg_isready -U "$DB_USER" -d postgres -q; do
    if [[ $(date +%s) -ge $deadline ]]; then
        echo "[bringup] FATAL: postgres failed healthcheck within 60s" >&2
        podman compose logs --tail=50 postgres >&2 || true
        exit 1
    fi
    sleep 1
done
echo "[bringup] postgres ready" >&2

# ---- 2. Reset DB ------------------------------------------------------------

echo "[bringup] resetting database $DB_NAME..." >&2
podman compose exec -T -e PGPASSWORD="$DB_PASSWORD" postgres \
    psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS $DB_NAME;" \
    -c "CREATE DATABASE $DB_NAME;" >&2
podman compose exec -T -e PGPASSWORD="$DB_PASSWORD" postgres \
    psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
    -c "CREATE EXTENSION IF NOT EXISTS vector;" >&2
echo "[bringup] database reset" >&2

# ---- 3. Render config -------------------------------------------------------

cat > "$CONFIG" <<EOF
# Auto-rendered by scripts/e2e/bringup.sh — DO NOT EDIT MANUALLY.
db_host: localhost
db_port: 5432
db_database: $DB_NAME
db_user: $DB_USER
db_password: $DB_PASSWORD
# Yielding-tools (M2+) holds one perpetual LISTEN connection plus
# several polling background tasks; min=1/max=5 deadlocks the
# lifespan on Windows. min=5/max=20 leaves comfortable headroom.
db_min_pool_size: 5
db_max_pool_size: 20

host: 127.0.0.1
port: $PORT

log_level: info
log_json: true
log_file: ./tests/.e2e/logs/matrix.log

scheduler:
  provider: postgres
  config: {}

EOF

if [[ "$NO_VECTOR" != "1" ]]; then
    cat >> "$CONFIG" <<EOF
vector_store:
  provider: pgvector
  config:
    hostname: localhost
    port: 5432
    database: $DB_NAME
    username: $DB_USER
    password: $DB_PASSWORD

EOF
else
    echo "[bringup] MATRIX_E2E_NO_VECTOR=1 — omitting vector_store block from config" >&2
fi

cat >> "$CONFIG" <<EOF
worker:
  concurrency: 4
  heartbeat_interval_seconds: 5
  lease_ttl_seconds: 15
  poll_interval_seconds: 1.0
  drain_timeout_seconds: 30
EOF
echo "[bringup] rendered $CONFIG" >&2

# ---- 4. Defensive: kill any leftover server from a previous crashed run -----

if [[ -f "$PID_FILE" ]]; then
    old_pid="$(cat "$PID_FILE" || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
        echo "[bringup] killing leftover server pid=$old_pid" >&2
        kill "$old_pid" 2>/dev/null || true
        sleep 2
        kill -9 "$old_pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
fi

# ---- 5. Launch matrix in background -----------------------------------------

echo "[bringup] launching matrix on port $PORT..." >&2
: > "$STDOUT_FILE"
(
    uv run matrix api --config "$CONFIG" --run-worker \
        > "$STDOUT_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    disown
) </dev/null

server_pid="$(cat "$PID_FILE")"
echo "[bringup] server started pid=$server_pid" >&2

# ---- 6. Poll health ---------------------------------------------------------

# 60s deadline accommodates slow Windows postgres handshakes when the
# pool pre-allocates min connections + the yielding-tools background
# tasks (M2+) initialise their pool conns. 30s was tight enough to
# race the lifespan on cold-cache laptops.
deadline=$(( $(date +%s) + 60 ))
while true; do
    if ! kill -0 "$server_pid" 2>/dev/null; then
        echo "[bringup] FATAL: server died during startup" >&2
        echo "--- last 100 lines of server.stdout ---" >&2
        tail -n 100 "$STDOUT_FILE" >&2 || true
        echo "--- last 100 lines of matrix.log ---" >&2
        [[ -f "$LOG_DIR/matrix.log" ]] && tail -n 100 "$LOG_DIR/matrix.log" >&2 || true
        exit 1
    fi
    if curl -fsS "http://127.0.0.1:$PORT/v1/health" > /dev/null 2>&1; then
        echo "READY"
        echo "[bringup] healthy after $((30 - (deadline - $(date +%s))))s" >&2
        # Hint to anything that sources this script: e2e tests are
        # opt-in via MATRIX_RUN_E2E=1. The runbook tells pytest about it
        # via the inline env-var prefix, but exporting it here is
        # harmless and saves a step for ad-hoc debugging.
        echo "export MATRIX_RUN_E2E=1" > "$E2E_DIR/env.sh"
        exit 0
    fi
    if [[ $(date +%s) -ge $deadline ]]; then
        echo "[bringup] FATAL: /v1/health did not respond within 60s" >&2
        echo "--- last 100 lines of server.stdout ---" >&2
        tail -n 100 "$STDOUT_FILE" >&2 || true
        echo "--- last 100 lines of matrix.log ---" >&2
        [[ -f "$LOG_DIR/matrix.log" ]] && tail -n 100 "$LOG_DIR/matrix.log" >&2 || true
        kill "$server_pid" 2>/dev/null || true
        exit 1
    fi
    sleep 1
done
