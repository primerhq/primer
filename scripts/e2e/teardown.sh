#!/usr/bin/env bash
# Tear down the primer test environment.
#
# The KILL sequence below is best-effort (whichever pid we have, SIGTERM
# then SIGKILL) — but exiting 0 is NOT unconditional any more (01a08b6f).
# Step 2 verifies teardown actually worked by checking the one property
# that matters — is anything still listening on $PORT — rather than by
# re-checking the same pid the kill sequence already used. A caller that
# gets exit 0 from this script can trust the port is free; a caller that
# gets exit 1 has a real problem to look at, not a false negative.

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
PORT="${PRIMER_E2E_PORT:-8765}"

# ---- 1. Stop the primer server (best-effort KILL) ---------------------------
#
# 01a08b6f: the pid in PID_FILE is bringup.sh's own `$!` after backgrounding
# `uv run primer api ...` — that pid is `uv run`'s OWN wrapper process, not
# the `primer api` interpreter it launches. Verified empirically: `uv run`
# FORKS a child for the real interpreter rather than exec-replacing itself
# into it (confirmed live — the captured pid and the pid that actually binds
# the port are different processes; `os.execve`-style replacement would make
# them the same). So `kill "$pid"` here relies entirely on `uv run` forwarding
# the signal to its child, which is today's OBSERVED behaviour, not a
# documented guarantee of `uv run`. That is exactly why this step's own
# `kill -0` checks are no longer this script's success criterion — see
# step 2, which checks the port instead of trusting this pid a second time.

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

# ---- 2. Verify by PORT, not by pid ------------------------------------------
#
# 01a08b6f (the actual fix, not just a comment): whether step 1's kill
# reached the real server or only its `uv run` wrapper, the property we
# actually care about is "is $PORT free" — checking that directly is robust
# to any process-tree shape (this pid, a grandchild, a future `uv` version
# that forks differently), where re-checking step 1's own pid would only
# ever prove the WRAPPER is gone. A killed LISTEN socket is released
# promptly but the kill syscall returning is not provably synchronous with
# that release, so poll briefly (5 x 1s) before treating a still-open port
# as a genuine survivor rather than a benign race.
port_still_held=1
for _ in $(seq 1 5); do
    if ! timeout 1 bash -c ">/dev/tcp/127.0.0.1/$PORT" 2>/dev/null; then
        port_still_held=0
        break
    fi
    sleep 1
done
if [[ "$port_still_held" -eq 1 ]]; then
    echo "[teardown] FATAL: port $PORT is still held after the kill sequence" \
        "- the primer server (or something else) survived teardown. This is" \
        "NOT a clean teardown; a subsequent bringup on this port cannot be" \
        "trusted to be running fresh code." >&2
    if command -v ss >/dev/null 2>&1; then
        ss -ltnp 2>/dev/null | grep ":$PORT " >&2 || true
    elif command -v lsof >/dev/null 2>&1; then
        lsof -i ":$PORT" >&2 || true
    fi
fi

# ---- 3. Bring Postgres down + drop its volume -------------------------------
#
# Still best-effort/`|| true`: a container-runtime failure here is a
# separate, already-accepted risk (podman/docker flakiness), not the
# false-success-on-a-surviving-server defect this fix targets. It runs
# regardless of step 2's finding so a caller always gets a clean
# container/volume state even when the host-process check below fails.

echo "[teardown] $RUNTIME compose down -v (drops the primer-pgdata volume)" >&2
$RUNTIME compose down -v >&2 || true

if [[ "$port_still_held" -eq 1 ]]; then
    echo "[teardown] FAILED: port $PORT still held, see diagnostics above" >&2
    exit 1
fi

echo "[teardown] done" >&2
exit 0
