---
slug: workers-and-health
title: Workers and health
section: features
summary: The worker pool, the health endpoint, the workers page, and how to tell a sick instance from a busy one.
---

## The pool tile

The dashboard's workers tile shows the live state of the worker
pool: how many are busy, how many are parked (holding a session
lease but in flight on a slow tool), how many are idle, and how
many failed.

```mockup:worker-stats
{ "total": 8, "busy": 5, "parked": 2, "failed": 0 }
```

A healthy pool runs around 30 to 70 percent utilisation. Pinned
near 100 percent means you are queueing work; pinned near 0
means you are over-provisioned.

```callout:warning
Oversubscribing the worker pool (utilisation pinned at 100% for
hours) does not just slow new work; it also delays parked
sessions resuming, because the claim engine has no free slots
to pick up the resume. Bump `worker.pool_size` or add a
dedicated worker process.
```

## Tuning the pool

Two knobs:

```code-tabs:bash
--- bash
# Pool size: how many concurrent tool dispatches one process can
# run. Default 8.
export PRIMER_WORKER__POOL_SIZE=16

# Lease TTL: how long a worker holds a session lease before the
# claim engine considers it dead. Default 60 seconds.
export PRIMER_WORKER__LEASE_TTL_SECONDS=120
```

The TTL trades fast failure recovery against false positives. A
short TTL recovers quickly when a worker crashes but cancels
legitimately-slow tool calls. The 60 second default is the
middle ground.

## The health endpoint

`/v1/health` returns the worker pool state, the storage backend
state, and the scheduler state. Use it as the readiness probe:

```code-tabs:bash,curl
--- bash
# Local probe:
curl -fs http://localhost:8000/v1/health | jq .

# Returns:
# {
#   "worker_pool": {"size": 8, "busy": 5, "parked": 2, "failed": 0},
#   "storage": {"kind": "postgres", "reachable": true},
#   "scheduler": {"kind": "postgres", "next_fire": "..."}
# }
--- curl
curl -fs http://localhost:8000/v1/health \
  || exit 1  # fail the deploy if anything is unhealthy
```

## When workers fail

A worker process flagged failed (red slice on the tile) means
the process exited unexpectedly. The pool restarts it
automatically; the failed count resets after a clean re-claim.
Persistent failures usually mean a recurring exception inside
a specific tool; the worker log shows the traceback.

## Separating API from worker

The default config runs API + worker in one process
(`PRIMER_RUNTIME_MODE=api+worker`). For larger deploys, run a
dedicated worker process with `PRIMER_RUNTIME_MODE=worker` and
keep the API process for serving HTTP. The two share storage; no
extra wiring is needed.
