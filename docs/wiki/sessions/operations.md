---
title: "Process health, workers and setup"
summary: "Inspect process capabilities and worker claims without confusing probes with completed work."
order: 8
related: [sessions/observe-a-session, workspaces/what-a-workspace-is, providers]
---

## What it is

The worker pool picks up runnable claims and executes sessions. Process health, database readiness, registered workers and actual session outcomes are different observations. A healthy API or a visible worker is not proof that a particular session completed.

## Concepts

- Health reports the serving process; readiness checks database reachability. Capabilities describe configured features, including optional speech backends.
- Worker lists and lane counters describe registered workers and process instruments. Admin drain stops that worker accepting new work so other workers can take over; removing a dead registry row does not execute or repair a session.
- Setup evaluates the install's predicates. Running the ensure pass and restoring seeded agent definitions are admin mutations, not read-only diagnostics; restoring defaults can replace an intentionally edited seeded definition.
- Read a session's durable row and transcript to verify its actual result. No probe overrides its ownership or workspace access rules.

## How to

1. Read health and readiness, then capabilities and the worker registry.
2. Use a session's turn log and runtime state to locate failed, queued or parked work. Keep the failure evidence before requesting a worker change.
3. Ask an admin to drain or remove only the intended worker when necessary. Never purge active workers merely because a session is slow.
4. Read setup predicates before a repair. Obtain an intentional decision before restoring seeded definitions rather than treating reset as a diagnostic command.

## Tool calls

The operation references below carry the live role floor and default approval facts. Read-only observations do not grant permission to drain, purge, seed or reset.

## Gotchas

- A liveness endpoint being reachable before login does not make its generated target an unauthenticated tool.
- Per-process lane counters are not a cross-worker completed-task ledger.
- A required approval still parks an in-platform mutation and refuses an external MCP call; an admin role alone does not erase that gate.

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`capabilities__get_capabilities`](reference/tools/capabilities/get_capabilities) | get_capabilities. | user |
| [`health__health`](reference/tools/health/health) | Process health. | user |
| [`health__ready`](reference/tools/health/ready) | Database reachability. | user |
| [`setup__get_setup_state`](reference/tools/setup/get_setup_state) | Live-evaluate all six setup predicates. | admin |
| [`setup__reset_agents`](reference/tools/setup/reset_agents) | Restore the default definitions of the seeded agents. | admin |
| [`setup__seed_now`](reference/tools/setup/seed_now) | Run the S5 ensure pass now. | admin |
| [`workers__delete_worker`](reference/tools/workers/delete_worker) | Remove a single dead worker from the registry. | admin |
| [`workers__drain_worker`](reference/tools/workers/drain_worker) | Mark a worker as draining (other workers take over its sessions). | admin |
| [`workers__list_workers`](reference/tools/workers/list_workers) | List registered workers. | user |
| [`workers__purge_dead_workers`](reference/tools/workers/purge_dead_workers) | Remove every dead worker from the registry. | admin |
| [`workers__worker_lane_stats`](reference/tools/workers/worker_lane_stats) | Per-lane task counters read from the in-process instruments. | user |

## Related

[Observe a session](sessions/observe-a-session), [workspace lifecycle](workspaces/what-a-workspace-is), [providers](providers).
