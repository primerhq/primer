-- Bootstrap extensions required by primer.
--
-- This file is mounted into /docker-entrypoint-initdb.d so it runs
-- exactly once: the first time the postgres container boots against
-- an empty data volume. Re-running ``docker compose up`` after the
-- volume already exists is a no-op (the directory is only walked when
-- the data dir is empty).
--
-- Drop the volume (``docker compose down -v``) and bring the container
-- back up to re-execute this script.

CREATE EXTENSION IF NOT EXISTS vector;
