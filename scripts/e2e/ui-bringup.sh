#!/usr/bin/env bash
# Bring up the matrix UI test environment.
#
# The UI loop runs pytest on the HOST against the matrix-app container
# (per the architecture decision in docs/testing/04-ui-test-loop.md).
# This script:
#
#   1. Ensures podman + the compose stack are up (postgres + matrix-app).
#   2. Verifies /console/ is reachable + serves index.html.
#   3. Ensures playwright + chromium are installed in the host venv.
#
# Exit code 0 means the environment is ready for `pytest tests/ui_e2e/`.
# Exit code 1 means something failed — relevant diagnostics dumped to
# stderr before exiting.
#
# Idempotent: if everything is already up + healthy, this is fast (~1 s).
# Database state is NOT reset between iterations — UI tests must clean
# up their own resources or use unique_suffix to avoid collisions.
# If you need a clean DB, run `podman compose down -v` first.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

PORT="${MATRIX_E2E_PORT:-8765}"
BASE_URL="http://127.0.0.1:$PORT"
CONSOLE_URL="$BASE_URL/console/"

# Windows-friendly PATH augmentation: when this script is invoked from
# pytest / a Claude Code Bash tool / a bare git-bash window, $PATH may
# not include the Windows app dirs where podman + docker-compose live.
# Prepend the common locations idempotently so `command -v` finds them.
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

# Detect podman vs docker — same trick scripts/e2e/bringup.sh uses.
if command -v podman >/dev/null 2>&1; then
    COMPOSE=(podman compose)
elif command -v docker >/dev/null 2>&1; then
    COMPOSE=(docker compose)
else
    echo "[ui-bringup] FATAL: neither podman nor docker found on PATH" >&2
    echo "[ui-bringup] searched: $PATH" >&2
    exit 1
fi

# ---- 1. Compose stack ------------------------------------------------------

echo "[ui-bringup] ensuring matrix-app + postgres are up..." >&2
"${COMPOSE[@]}" up -d >&2

# ---- 2. Wait for /v1/health and /console/ to respond -----------------------

deadline=$(( $(date +%s) + 60 ))
until curl -fsS "$BASE_URL/v1/health" > /dev/null 2>&1; do
    if [[ $(date +%s) -ge $deadline ]]; then
        echo "[ui-bringup] FATAL: /v1/health did not respond within 60s" >&2
        "${COMPOSE[@]}" logs --tail=50 matrix >&2 || true
        exit 1
    fi
    sleep 1
done

if ! curl -fsS "$CONSOLE_URL" | grep -q "<title>Matrix"; then
    echo "[ui-bringup] FATAL: /console/ did not serve the expected index.html" >&2
    curl -sSI "$CONSOLE_URL" >&2 || true
    exit 1
fi

echo "[ui-bringup] matrix server ready at $CONSOLE_URL" >&2

# ---- 3. Ensure playwright + chromium are installed -------------------------

# We invoke playwright via the venv's python so the version pinned in
# pyproject.toml is used. `playwright install --with-deps chromium` is
# idempotent — fast no-op when the browser is already there.
if [[ -x ".venv/Scripts/python.exe" ]]; then
    PY=".venv/Scripts/python.exe"
elif [[ -x ".venv/bin/python" ]]; then
    PY=".venv/bin/python"
else
    echo "[ui-bringup] FATAL: project venv not found at .venv/ — run 'uv sync' first" >&2
    exit 1
fi

if ! "$PY" -c "import playwright" 2>/dev/null; then
    echo "[ui-bringup] installing playwright + pytest-playwright via uv sync..." >&2
    uv sync --group dev >&2
fi

# Install chromium silently if missing. Use --dry-run-style flag pattern:
# playwright install is fast (~1 s) when the browser is already cached.
echo "[ui-bringup] ensuring chromium is installed (idempotent)..." >&2
"$PY" -m playwright install chromium >&2

echo "READY"
echo "[ui-bringup] healthy after $((60 - (deadline - $(date +%s))))s" >&2
exit 0
