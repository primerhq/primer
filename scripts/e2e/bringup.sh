#!/usr/bin/env bash
# Bring up the matrix test environment.
#
# Container runtime: Podman or Docker. The script auto-detects which one
# is on $PATH (podman first, then docker) and uses its `compose`
# subcommand. Override with PRIMER_E2E_CONTAINER_RUNTIME=podman|docker
# if both are installed and you need to pin one explicitly.
#
# Steps:
#   1. $RUNTIME compose up -d, wait for postgres healthcheck
#   2. drop+recreate the matrix_e2e database with the pgvector extension
#   3. render tests/.e2e/config.yaml
#   4. launch `uv run matrix api` in the background (worker runs by default)
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

# Container-runtime autodetect. PRIMER_E2E_CONTAINER_RUNTIME pins it
# explicitly; otherwise we prefer podman (the documented default) and
# fall back to docker. Either way the variable expands to the binary
# name; both have a `compose` subcommand against the same compose file.
RUNTIME="${PRIMER_E2E_CONTAINER_RUNTIME:-}"
if [[ -z "$RUNTIME" ]]; then
    if command -v podman >/dev/null 2>&1; then
        RUNTIME="podman"
    elif command -v docker >/dev/null 2>&1; then
        RUNTIME="docker"
    else
        echo "[bringup] FATAL: neither podman nor docker on PATH" >&2
        exit 1
    fi
fi
echo "[bringup] using container runtime: $RUNTIME" >&2

E2E_DIR="$ROOT/tests/.e2e"
LOG_DIR="$E2E_DIR/logs"
CONFIG="$E2E_DIR/config.yaml"
PID_FILE="$E2E_DIR/server.pid"
STDOUT_FILE="$E2E_DIR/server.stdout"

PORT="${PRIMER_E2E_PORT:-8765}"
DB_USER="${PRIMER_DB_USER:-matrix}"
DB_PASSWORD="${PRIMER_DB_PASSWORD:-matrix}"
DB_NAME="matrix_e2e"
# Set PRIMER_E2E_NO_VECTOR=1 to render an AppConfig without a vector_store
# block. Used by gating tests that need to assert 503 behaviour on the
# collection / document / search routes when the subsystem is disabled.
NO_VECTOR="${PRIMER_E2E_NO_VECTOR:-0}"

mkdir -p "$E2E_DIR" "$LOG_DIR"

# ---- 1. Docker --------------------------------------------------------------

echo "[bringup] starting postgres container..." >&2
$RUNTIME compose up -d postgres >&2

echo "[bringup] waiting for postgres healthcheck..." >&2
deadline=$(( $(date +%s) + 60 ))
until $RUNTIME compose exec -T postgres pg_isready -U "$DB_USER" -d postgres -q; do
    if [[ $(date +%s) -ge $deadline ]]; then
        echo "[bringup] FATAL: postgres failed healthcheck within 60s" >&2
        $RUNTIME compose logs --tail=50 postgres >&2 || true
        exit 1
    fi
    sleep 1
done
echo "[bringup] postgres ready" >&2

# ---- 2. Reset DB ------------------------------------------------------------

echo "[bringup] resetting database $DB_NAME..." >&2
$RUNTIME compose exec -T -e PGPASSWORD="$DB_PASSWORD" postgres \
    psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS $DB_NAME;" \
    -c "CREATE DATABASE $DB_NAME;" >&2
$RUNTIME compose exec -T -e PGPASSWORD="$DB_PASSWORD" postgres \
    psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
    -c "CREATE EXTENSION IF NOT EXISTS vector;" >&2
echo "[bringup] database reset" >&2

# ---- 3. Render config -------------------------------------------------------

cat > "$CONFIG" <<EOF
# Auto-rendered by scripts/e2e/bringup.sh — DO NOT EDIT MANUALLY.
db:
  provider: postgres
  config:
    hostname: localhost
    port: 5432
    database: $DB_NAME
    username: $DB_USER
    password: $DB_PASSWORD
    # Yielding-tools (M2+) holds one perpetual LISTEN connection plus
    # several polling background tasks; min=1/max=5 deadlocks the
    # lifespan on Windows. min=5/max=20 leaves comfortable headroom.
    pool:
      min_size: 5
      max_size: 20

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
    echo "[bringup] PRIMER_E2E_NO_VECTOR=1 — omitting vector_store block from config" >&2
fi

cat >> "$CONFIG" <<EOF
worker:
  concurrency: 4
  heartbeat_interval_seconds: 5
  lease_ttl_seconds: 15
  poll_interval_seconds: 1.0
  drain_timeout_seconds: 30

# MCP stdio safety: when set (non-null), MCP toolsets whose stdio
# command[0] is not in this list are refused at session-open with
# ConfigError → 503 /errors/service-unavailable. Pinned for T0245.
# Includes npx so T0767 (open-websearch) still runs; any binary
# outside this list is rejected.
mcp_stdio_allowed_commands:
  - npx
  - python
  - uv
EOF
echo "[bringup] rendered $CONFIG" >&2

# ---- 4. Defensive: stop the docker-compose matrix-app container if it's
#         running. The UI loop's bring-up leaves the container alive between
#         iterations (per docs/testing/04-ui-test-loop.md Phase 6 step 1) on
#         the same host port we use here. The container's image is built
#         from `./Dockerfile` at compose-up time, so it may carry older code
#         than the host's working tree — letting it win the bind race causes
#         spurious 404s when the host's newer routes aren't present.
#         This stop is idempotent: no-op when the container isn't up.
if "$RUNTIME" compose ps --services --filter "status=running" 2>/dev/null | grep -qx "matrix"; then
    echo "[bringup] stopping leftover matrix-app container so the host process can bind $PORT..." >&2
    "$RUNTIME" compose stop matrix >&2 || true
fi

# ---- 5. Defensive: kill any leftover host server from a previous crashed run

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
    uv run matrix api --config "$CONFIG" \
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
        # opt-in via PRIMER_RUN_E2E=1. The runbook tells pytest about it
        # via the inline env-var prefix, but exporting it here is
        # harmless and saves a step for ad-hoc debugging.
        echo "export PRIMER_RUN_E2E=1" > "$E2E_DIR/env.sh"
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
