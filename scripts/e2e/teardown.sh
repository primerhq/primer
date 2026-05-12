#!/usr/bin/env bash
# Tear down the matrix test environment.
#
# Always exits 0 — teardown is best-effort. The loop relies on this to be
# safe to call after a failed bringup.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

E2E_DIR="$ROOT/tests/.e2e"
PID_FILE="$E2E_DIR/server.pid"

# ---- 1. Stop the matrix server ---------------------------------------------

if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE" || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        echo "[teardown] stopping matrix pid=$pid" >&2
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

echo "[teardown] podman compose down -v (drops the matrix-pgdata volume)" >&2
podman compose down -v >&2 || true

echo "[teardown] done" >&2
exit 0
