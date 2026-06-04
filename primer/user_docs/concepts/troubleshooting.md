---
slug: troubleshooting
title: Troubleshooting
section: concepts
summary: Common startup errors, how to read primer log lines, and what to grep for.
---

## The log line format

Primer logs at INFO by default. Every line carries the logger name
in square-bracket-style, which gives you the grep handle. Examples
of what the logger names map to:

- `primer.api.app` -- lifespan startup, router mount, doc lint.
- `primer.user_docs_service` -- doc index walk, hot-reload misses.
- `primer.scheduler.*` -- trigger fires, claim handoffs.
- `primer.workspace.probe` -- workspace health probe results.

```callout:info
Bump the log level by exporting `PRIMER_LOG_LEVEL=DEBUG` before
start. Debug surfaces every storage call, which is noisy but the
right level when chasing a 'where does this row come from' bug.
```

## Port already in use

```
ERROR: [Errno 98] Address already in use
```

Something is already on `PRIMER_PORT` (default 8000). Find it:

```code-tabs:bash
--- bash
# Linux
lsof -i :8000

# macOS
lsof -nP -iTCP:8000 | grep LISTEN
```

Kill it or pick a different port: `export PRIMER_PORT=8001`.

## Doc lint blocked startup

```
RuntimeError: user_docs: 3 lint error(s); refusing to start.
  primer/user_docs/features/agents.md:42 [no_em_dash] ...
  primer/user_docs/cookbook/x.md:? [missing_frontmatter_key] ...
```

Strict mode is on (`PRIMER_USER_DOCS_STRICT=1`) and at least one
user doc fails lint. The summary names the file and the rule. Fix
the doc and restart; or unset the strict flag to demote errors to
log warnings.

```callout:warning
The strict gate is there for a reason. Production should run
without strict and rely on the operator-side lint sweep; dev
should run with strict so authoring mistakes do not silently drop
a doc from the manifest.
```

## Workspace stuck in 'failed'

The workspace probe loop flips a workspace into `failed` after
three consecutive missed pings. Common causes:

- The workspace process exited (out-of-memory or a crash on a tool
  call).
- Network egress is blocked and the agent tried to call a remote
  endpoint.
- The workspace template's resource caps were too tight.

```code-tabs:bash,python
--- bash
# Read the workspace's recent log via the API:
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/v1/workspaces/$WSID/log?lines=200
--- python
ws = client.workspaces.get(ws_id)
for line in client.workspaces.log(ws.id, lines=200):
    print(line.timestamp, line.level, line.message)
```

## A session is parked but no approval is showing

Tool approvals fire to the agent's configured channel. If no
channel is configured for the agent, the prompt lands in the
approvals queue on the IC bell instead.

```callout:danger
A parked session that never resolves blocks the worker slot. Watch
the workers page; if utilisation pins near 100% you may have an
approval queue piling up. Approve, reject, or cancel the parked
session to free the slot.
```

## Auth 401 immediately after upgrade

The session secret rotated. The console silently reissues a cookie
on next login, but bearer-token clients see 401 until the operator
mints new tokens.

```code-tabs:bash
--- bash
# Mint a new token via the API tokens page in the console, then
# update the env var your client reads.
export PRIMER_TOKEN=<new-token>
```
