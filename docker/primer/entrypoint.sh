#!/bin/sh
# Container entrypoint for the primer service.
#
# Renders /app/config.yaml from PRIMER_* environment variables (set by
# docker-compose.yml or `podman run -e ...`), then exec's the CMD. The
# primer CLI requires --config; this script lets us drive the config
# entirely via env vars in compose without baking credentials into the
# image.
#
# The db block uses the nested provider/config shape that AppConfig
# (StorageProviderConfig) actually reads. The old flat db_host/db_port/...
# keys are silently ignored by AppConfig (extra="ignore") and cause a
# silent fallback to embedded SQLite.

set -e

: "${PRIMER_DB_HOST:=postgres}"
: "${PRIMER_DB_PORT:=5432}"
: "${PRIMER_DB_DATABASE:=primer}"
: "${PRIMER_DB_USER:=primer}"
: "${PRIMER_DB_PASSWORD:=primer}"
: "${PRIMER_HOST:=0.0.0.0}"
: "${PRIMER_PORT:=8765}"
: "${PRIMER_LOG_LEVEL:=info}"
: "${PRIMER_LOG_JSON:=true}"
: "${PRIMER_WORKER_CONCURRENCY:=4}"

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

exec "$@"
