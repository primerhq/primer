---
slug: upgrading
title: Upgrading primer
section: getting-started
summary: How to upgrade primer in place without losing state, and the breaking-change checklist.
---

## The two flavours of upgrade

Pick the procedure that matches how you installed primer.

```code-tabs:bash,docker
--- bash
# Local install (uv-managed).
cd /path/to/primer
git fetch && git pull
uv sync
# Restart your process (systemd, supervisord, etc.).
sudo systemctl restart primer
--- docker
# Docker install.
docker pull ghcr.io/codemug/primer:latest
docker stop primer && docker rm primer
docker run -d --name primer \
  -p 8000:8000 \
  -v $HOME/.primer:/data \
  ghcr.io/codemug/primer:latest
```

## State that survives the upgrade

Both procedures preserve:

- The configured database (SQLite file or Postgres rows).
- The session secret on disk (`$PRIMER_DATA/secrets/session.key`).
- The bug reports directory (`bugs/`).
- The committed user-doc tree (`primer/user_docs/`).

State that does NOT survive:

- In-flight sessions and chats. Restart them after the upgrade.
- The in-process worker pool (drained at shutdown).
- The in-memory scheduler (rebuilds from storage on next start).

## The breaking-change checklist

Before pulling a release tag, run this checklist:

```callout:warning
Read the release notes. Primer follows semver; a minor bump
(0.x.0) may still ship a breaking config rename or REST shape
change. Skipping the release notes is the most common cause of a
post-upgrade outage.
```

1. Snapshot the database. For Postgres, `pg_dump` the schema. For
   SQLite, copy the file.
2. Snapshot `$PRIMER_DATA/secrets/`.
3. Note your current version: `uv run primer --version`.
4. Pull the new code or image.
5. Run the smoke endpoint: `curl http://localhost:8000/v1/version`.
6. Re-verify auth: hit a known authenticated endpoint with your
   bearer token.

```callout:danger
Downgrading is not supported once the new code has run any
migration. Restore from the snapshot if a downgrade is needed.
```

## CI smoke

For unattended deploys, the post-restart smoke is:

```code-tabs:bash
--- bash
# /v1/health returns the worker pool + storage health.
curl -fs http://localhost:8000/v1/health || exit 1

# /v1/version returns build metadata.
curl -fs http://localhost:8000/v1/version || exit 1

# Authenticated probe (replace $TOKEN with a long-lived API token).
curl -fs -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/v1/agents?limit=1 || exit 1
```
