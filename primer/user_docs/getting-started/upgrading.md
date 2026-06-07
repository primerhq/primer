---
slug: upgrading
title: Upgrading primer
section: getting-started
summary: How to upgrade a primer deployment safely -- back up storage, apply migrations, restart services, and verify health.
---

## Before you start

Every upgrade should follow the same three-phase sequence: back up, restart, verify. Skipping any phase risks unrecoverable state loss or a silent broken deployment.

```callout:warning
Read the release notes before pulling new code or a new image. Primer follows semver; a minor bump (0.x.0) can still ship a breaking configuration rename or REST shape change. Most post-upgrade outages trace back to skipped release notes.
```

## Phase 1 -- back up

Two pieces of state must be snapshotted before any upgrade:

**Database.** For Postgres, run `pg_dump` to capture the full schema and data. For SQLite, copy the database file to a safe location while the process is stopped.

**Secrets directory.** Copy `$PRIMER_DATA/secrets/` in full. The session signing key lives here; losing it invalidates all existing sessions and forces all users to log in again.

Note your current version before proceeding:

```code-tabs:bash
--- bash
uv run primer --version
```

```callout:danger
Downgrading is not supported once the new binary has run any migration. If an upgrade goes wrong, restore from the snapshot taken above -- do not attempt to roll back by reverting the code alone.
```

## Phase 2 -- apply the upgrade and restart

Pull the new code or image, then restart the API process and all workers.

```code-tabs:bash,docker
--- bash
# Local install managed with uv.
cd /path/to/primer
git fetch && git pull
uv sync
# Restart the API (adjust for your process manager).
sudo systemctl restart primer-api
# Restart workers if they run as a separate process.
sudo systemctl restart primer-worker
--- docker
# Docker install.
docker pull ghcr.io/codemug/primer:latest
docker stop primer && docker rm primer
docker run -d --name primer \
  -p 8000:8000 \
  -v $HOME/.primer:/data \
  ghcr.io/codemug/primer:latest
```

Primer applies schema migrations automatically on startup. The process will not serve requests until migrations complete. Watch the startup log for a migration summary line before proceeding to Phase 3.

## Phase 3 -- verify

Run the smoke sequence against the restarted process:

```code-tabs:bash
--- bash
# Health endpoint: returns worker pool + storage status.
curl -fs http://localhost:8000/v1/health || exit 1

# Version endpoint: returns build metadata including the new version.
curl -fs http://localhost:8000/v1/version || exit 1

# Authenticated probe -- replace $TOKEN with a long-lived API token.
curl -fs -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/v1/agents?limit=1 || exit 1
```

```callout:info
The `/v1/health` response includes a `worker_pool` object with `capacity` and `in_flight` fields. A healthy deployment shows `capacity` greater than zero and no `storage` errors. The Workers page in the console shows the same data refreshed every 2 seconds.
```

Open the console and navigate to Health (Operations group in the sidebar) to confirm the live health graph is green. The page polls `/v1/health` every 5 seconds and maintains a client-side history.

## State that survives an upgrade

The following state is preserved across restarts and upgrades:

- The configured database (SQLite file or Postgres rows, post-migration).
- The session signing key (`$PRIMER_DATA/secrets/session.key`).
- The bug reports directory (`bugs/`).
- The user-doc source tree (`primer/user_docs/`).

State that does not survive:

- In-flight sessions and chats (restart them after the upgrade).
- The in-process worker pool (drained at shutdown and rebuilt on start).
- The in-memory scheduler (rebuilt from storage on next start).

```ref:reference/cli
Full CLI reference including the `--version` and `--no-worker` flags.
```

```ref:features/workers-and-health
Reading the health endpoint and understanding worker pool metrics.
```
