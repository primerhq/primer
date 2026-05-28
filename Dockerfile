# Image for the primer API server (+ embedded worker).
#
# Built by `podman compose build primer` (or `--build` on `up`). Used
# by the `primer` service in docker-compose.yml. The companion
# .dockerignore keeps the build context small.
#
# Layer strategy:
#   1. system deps + uv binary           — rarely changes
#   2. pyproject.toml + uv.lock          — changes on dep bumps
#   3. uv sync --no-install-project      — slow; cached on dep bumps only
#   4. project source                    — changes on every code edit
#   5. uv sync (installs project itself) — fast
#
# The primer package is installed editable (uv's default for `uv sync`)
# because `primer/api/app.py` resolves the UI directory via
# `Path(__file__).parent.parent.parent / "ui"`. With an editable
# install that resolves to /app/ui (correct); a non-editable install
# would resolve to a site-packages path that has no `ui/` subtree.

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# System deps:
# - curl              the HEALTHCHECK and for installing uv
# - ca-certificates   HTTPS during uv install + runtime fetches
# - git               required by primer/workspace/local/state.py:LocalStateRepo
#                     which uses `git init` / `git commit` for the
#                     workspace's .state/ versioned-state repo. Workspace
#                     creation (POST /v1/workspaces) and all graph-bound
#                     session dispatch fail without it.
# - libatomic1        required by the regopy shared-object (the Rego
#                     evaluator for ToolApprovalPolicy(type="policy")).
#                     Without it, `import regopy` raises OSError from
#                     ctypes.LoadLibrary and every policy-type create
#                     leaks as 500 instead of the intended 422. Surfaced
#                     by U0114 (UI loop) on the primer-app image.
# Most python deps (asyncpg, grpcio, pgvector, torch, transformers)
# ship manylinux wheels for cp313, so no build toolchain is needed.
# Add `build-essential` here if a future dep requires compilation.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Install uv (standalone binary, ~25 MB) directly to /usr/local/bin so
# it's on PATH for all subsequent layers.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# ----- Layer 2/3: dependency install (cached on pyproject+lock) -----
# README.md is referenced by pyproject.toml's `readme = "README.md"`
# field, so hatchling needs it present even for a deps-only sync.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# ----- Layer 4: project source -----
COPY primer ./primer
COPY ui ./ui
COPY docker/primer/entrypoint.sh /usr/local/bin/primer-entrypoint.sh
RUN chmod +x /usr/local/bin/primer-entrypoint.sh

# ----- Layer 5: install the project itself -----
# Editable install (uv default) so /app/primer is the live tree —
# necessary for the console mount's `_UI_DIR` path math.
RUN uv sync --frozen --no-dev

EXPOSE 8765

# Health: the FastAPI app exposes /v1/health (used by scripts/e2e/bringup.sh).
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
    CMD curl -fsS http://127.0.0.1:8765/v1/health || exit 1

# Entrypoint renders /app/config.yaml from PRIMER_* env vars, then
# exec's CMD. The primer CLI requires --config; the rendered file is
# always used.
ENTRYPOINT ["/usr/local/bin/primer-entrypoint.sh"]

# Default command. Override at `podman run` time or via compose
# `command:` if you want a non-worker process (e.g. `primer api
# --no-worker` for an API-only node, or `primer worker` alone). Default
# is api+worker (single-process).
CMD ["primer", "api", "--config", "/app/config.yaml"]
