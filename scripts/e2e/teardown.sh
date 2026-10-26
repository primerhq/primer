#!/usr/bin/env bash
# Tear down the primer test environment.
#
# DEFAULT (non-destructive): stops the host e2e server and leaves the shared
# Postgres container, compose network and the primer-pgdata volume running.
# The e2e suite reuses the shared dev Postgres (scripts/e2e/bringup.sh) on a
# separate primer_e2e database, so the default must NOT drop the volume (it
# would wipe the dogfood data). This was the silent `compose down -v` default
# that the C1 concern flagged; it is gone.
#
# DESTRUCTIVE (opt-in, separately named): `--destructive` together with
# PRIMER_E2E_DELETE_VOLUMES=1. This runs `compose down -v` (drops the volume).
# It is REFUSED when the resolved Postgres container/volume matches the shared
# dogfood environment (the e2e default), because dropping it would wipe the
# dogfood data.
#
# --print-args: print the generated compose args for the current mode without
# executing anything (used by the regression test to assert the -v behaviour).
#
# The KILL sequence below is best-effort (whichever pid we have, SIGTERM
# then SIGKILL) - but exiting 0 is NOT unconditional any more (01a08b6f).
# Step 2 verifies teardown actually worked by checking the one property
# that matters - is anything still listening on $PORT - rather than by
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

# ---- Argument parsing -------------------------------------------------------
# --destructive is the separately named destructive path; it only takes effect
# together with the explicit PRIMER_E2E_DELETE_VOLUMES=1 opt-in. --print-args
# prints the generated compose args (for the regression test) and exits.
DESTRUCTIVE=0
PRINT_ARGS=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --destructive) DESTRUCTIVE=1; shift ;;
        --print-args) PRINT_ARGS=1; shift ;;
        *) echo "[teardown] FATAL: unknown argument: $1" >&2; exit 2 ;;
    esac
done

# The compose args for the Postgres teardown. The default mode keeps the shared
# container/volume (no -v); the destructive mode adds -v to drop the volume.
compose_down_args() {
    if [[ "$DESTRUCTIVE" -eq 1 && "${PRIMER_E2E_DELETE_VOLUMES:-0}" == "1" ]]; then
        echo "down -v"
    else
        echo "down"
    fi
}

# Refuse destructive cleanup when the resolved Postgres container/volume matches
# the shared dogfood environment (the e2e default). The e2e suite reuses the
# shared dev Postgres on a separate primer_e2e database, so the container and
# volume are the dogfood ones; dropping them would wipe the dogfood data. A
# caller running --destructive against an ISOLATED e2e Postgres (a different
# compose project / container / volume) is not refused.
refuse_if_dogfood() {
    if [[ "$DESTRUCTIVE" -ne 1 || "${PRIMER_E2E_DELETE_VOLUMES:-0}" != "1" ]]; then
        return 0
    fi
    local compose_file="$ROOT/docker-compose.yml"
    if [[ ! -f "$compose_file" ]]; then
        echo "[teardown] REFUSING destructive cleanup: cannot resolve the Postgres" >&2
        echo "  container/volume (no $compose_file); not dropping volumes without a" >&2
        echo "  resolvable, unambiguous target." >&2
        return 1
    fi
    local container_name volume_name
    container_name="$(grep -E '^[[:space:]]+container_name:' "$compose_file" | grep -v 'primer-app' | head -1 | awk '{print $2}')"
    volume_name="$(grep -E '^[[:space:]]+name:' "$compose_file" | head -1 | awk '{print $2}')"
    if [[ "$container_name" == "primer-postgres" || "$volume_name" == "primer-pgdata" ]]; then
        echo "[teardown] REFUSING destructive cleanup: the resolved Postgres container/volume" >&2
        echo "  ($container_name / $volume_name) is the SHARED dogfood environment. The e2e" >&2
        echo "  suite reuses this volume on a separate primer_e2e database, so dropping it" >&2
        echo "  would wipe the dogfood data. Run --destructive only against an ISOLATED" >&2
        echo "  e2e Postgres (a different compose project)." >&2
        return 1
    fi
    return 0
}

# ---- --print-args (for the regression test) ---------------------------------
if [[ "$PRINT_ARGS" -eq 1 ]]; then
    compose_down_args
    exit 0
fi

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

# ---- 3. Bring Postgres down (ONLY in the destructive mode) ------------------
#
# The default mode leaves the shared Postgres container, compose network and
# volume running (the e2e suite reuses the shared dev Postgres on a separate
# primer_e2e database). The destructive mode (gated on --destructive +
# PRIMER_E2E_DELETE_VOLUMES=1) runs `compose down -v`, but is REFUSED when the
# resolved container/volume matches the shared dogfood environment. Still
# best-effort/`|| true` for the runtime call itself: a container-runtime failure
# is a separate, already-accepted risk (podman/docker flakiness). It runs
# regardless of step 2's finding so a caller always gets a clean container state
# even when the host-process check below fails.
if [[ "$DESTRUCTIVE" -eq 1 && "${PRIMER_E2E_DELETE_VOLUMES:-0}" == "1" ]]; then
    refuse_if_dogfood || exit 1
    down_args="$(compose_down_args)"
    echo "[teardown] $RUNTIME compose $down_args (DESTRUCTIVE: drops the primer-pgdata volume)" >&2
    $RUNTIME compose $down_args >&2 || true
else
    echo "[teardown] leaving the shared Postgres container/volume running (default, non-destructive)" >&2
fi

if [[ "$port_still_held" -eq 1 ]]; then
    echo "[teardown] FAILED: port $PORT still held, see diagnostics above" >&2
    exit 1
fi

echo "[teardown] done" >&2
exit 0
