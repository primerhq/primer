#!/usr/bin/env bash
# Tear down the primer test environment.
#
# Always exits 0 — teardown is best-effort. The loop relies on this to be
# safe to call after a failed bringup.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

# Container-runtime autodetect (mirror of bringup.sh).
RUNTIME="${PRIMER_E2E_CONTAINER_RUNTIME:-}"
if [[ -z "$RUNTIME" ]]; then
    if command -v podman >/dev/null 2>&1; then
        RUNTIME="podman"
    elif command -v docker >/dev/null 2>&1; then
        RUNTIME="docker"
    else
        RUNTIME="podman"
    fi
fi

E2E_DIR="$ROOT/tests/.e2e"
PID_FILE="$E2E_DIR/server.pid"

# ---- 1. Stop the primer server ---------------------------------------------

if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE" || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        echo "[teardown] stopping primer pid=$pid" >&2
        kill "$pid" 2>/dev/null || true
        # Wait up to 15 s for graceful shutdown.
        for _ in $(seq 1 15); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "[teardown] SIGKILL pid=$pid (did not exit gracefully)" >&2
            kill -9 "$pid" 2>/dev/null || true
        fi
    fi
    rm -f "$PID_FILE"
fi

# ---- 2. Bring Postgres down + drop its volume -------------------------------

echo "[teardown] $RUNTIME compose down -v (drops the primer-pgdata volume)" >&2
$RUNTIME compose down -v >&2 || true

echo "[teardown] done" >&2
exit 0
