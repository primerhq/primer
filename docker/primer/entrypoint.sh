#!/bin/sh
# Container entrypoint for the primer service.
#
# Renders /app/config.yaml from PRIMER_* environment variables, then
# exec's the CMD. Two modes, chosen by whether a Postgres host is given:
#
#   * Postgres mode (compose / `docker run -e PRIMER_DB_HOST=...`):
#     renders a postgres + pgvector + postgres-scheduler config. This is
#     the multi-process / production shape.
#   * Standalone mode (a bare `docker run` with no DB env): renders a
#     zero-config embedded-SQLite config (in-memory scheduler, no vector
#     store) so the image runs with no external services, matching
#     `pipx install primer && primer api`.
#
# The db block uses the nested provider/config shape that AppConfig
# (StorageProviderConfig) actually reads. The old flat db_host/db_port/...
# keys are silently ignored by AppConfig (extra="ignore").

set -e

: "${PRIMER_HOST:=0.0.0.0}"
: "${PRIMER_PORT:=8000}"
: "${PRIMER_LOG_LEVEL:=info}"
: "${PRIMER_LOG_JSON:=true}"
: "${PRIMER_WORKER_CONCURRENCY:=4}"

if [ -n "${PRIMER_DB_HOST:-}" ] || [ -n "${PRIMER_DB__CONFIG__HOSTNAME:-}" ]; then
    # ---- Postgres mode -----------------------------------------------------
    : "${PRIMER_DB_HOST:=postgres}"
    : "${PRIMER_DB_PORT:=5432}"
    : "${PRIMER_DB_DATABASE:=primer}"
    : "${PRIMER_DB_USER:=primer}"
    : "${PRIMER_DB_PASSWORD:=primer}"

    cat > /app/config.yaml <<EOF
db:
  provider: postgres
  config:
    hostname: ${PRIMER_DB_HOST}
    port: ${PRIMER_DB_PORT}
    database: ${PRIMER_DB_DATABASE}
    username: ${PRIMER_DB_USER}
    password: ${PRIMER_DB_PASSWORD}

host: ${PRIMER_HOST}
port: ${PRIMER_PORT}

log_level: ${PRIMER_LOG_LEVEL}
log_json: ${PRIMER_LOG_JSON}

vector_store:
  provider: pgvector
  config:
    hostname: ${PRIMER_DB_HOST}
    port: ${PRIMER_DB_PORT}
    database: ${PRIMER_DB_DATABASE}
    username: ${PRIMER_DB_USER}
    password: ${PRIMER_DB_PASSWORD}

scheduler:
  provider: postgres
  config: {}

worker:
  concurrency: ${PRIMER_WORKER_CONCURRENCY}
  heartbeat_interval_seconds: 5
  lease_ttl_seconds: 15
  poll_interval_seconds: 1.0
  drain_timeout_seconds: 30
EOF
else
    # ---- Standalone mode (embedded SQLite, no external services) -----------
    : "${PRIMER_SQLITE_PATH:=/app/data/data.sqlite}"
    mkdir -p "$(dirname "${PRIMER_SQLITE_PATH}")"

    cat > /app/config.yaml <<EOF
db:
  provider: sqlite
  config:
    path: ${PRIMER_SQLITE_PATH}

host: ${PRIMER_HOST}
port: ${PRIMER_PORT}

log_level: ${PRIMER_LOG_LEVEL}
log_json: ${PRIMER_LOG_JSON}

scheduler:
  provider: in_memory
  config: {}

worker:
  concurrency: ${PRIMER_WORKER_CONCURRENCY}
  heartbeat_interval_seconds: 5
  lease_ttl_seconds: 15
  poll_interval_seconds: 1.0
  drain_timeout_seconds: 30
EOF
fi

exec "$@"
