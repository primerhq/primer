#!/bin/sh
# Container entrypoint for the matrix service.
#
# Renders /app/config.yaml from MATRIX_* environment variables (set by
# docker-compose.yml or `podman run -e ...`), then exec's the CMD. The
# matrix CLI requires --config; this script lets us drive the config
# entirely via env vars in compose without baking credentials into the
# image.

set -e

: "${MATRIX_DB_HOST:=postgres}"
: "${MATRIX_DB_PORT:=5432}"
: "${MATRIX_DB_DATABASE:=matrix}"
: "${MATRIX_DB_USER:=matrix}"
: "${MATRIX_DB_PASSWORD:=matrix}"
: "${MATRIX_HOST:=0.0.0.0}"
: "${MATRIX_PORT:=8765}"
: "${MATRIX_LOG_LEVEL:=info}"
: "${MATRIX_LOG_JSON:=true}"
: "${MATRIX_WORKER_CONCURRENCY:=4}"

cat > /app/config.yaml <<EOF
db_host: ${MATRIX_DB_HOST}
db_port: ${MATRIX_DB_PORT}
db_database: ${MATRIX_DB_DATABASE}
db_user: ${MATRIX_DB_USER}
db_password: ${MATRIX_DB_PASSWORD}
db_min_pool_size: 1
db_max_pool_size: 10

host: ${MATRIX_HOST}
port: ${MATRIX_PORT}

log_level: ${MATRIX_LOG_LEVEL}
log_json: ${MATRIX_LOG_JSON}

vector_store:
  provider: pgvector
  config:
    hostname: ${MATRIX_DB_HOST}
    port: ${MATRIX_DB_PORT}
    database: ${MATRIX_DB_DATABASE}
    username: ${MATRIX_DB_USER}
    password: ${MATRIX_DB_PASSWORD}

scheduler:
  provider: postgres
  config: {}

worker:
  concurrency: ${MATRIX_WORKER_CONCURRENCY}
  heartbeat_interval_seconds: 5
  lease_ttl_seconds: 15
  poll_interval_seconds: 1.0
  drain_timeout_seconds: 30
EOF

exec "$@"
