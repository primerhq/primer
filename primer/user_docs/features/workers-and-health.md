---
slug: workers-and-health
title: Workers and health
section: features
summary: Read the workers page in the console -- worker pool status, in-flight capacity, scheduler state, and how to drain a worker.
---

## Overview

The Workers page shows every worker process the pool knows about, live metrics on what each one is doing, and controls to gracefully drain a worker before taking it out of service. The page polls every two seconds.

```embed:workers-stats
```

## Reading the summary strip

Four tiles across the top of the page give a quick pool snapshot:

| Tile | What it shows |
|---|---|
| Total | Every registered worker, including ones currently draining. |
| Active | Workers ready to accept new work. The sub-label shows how many are draining. |
| Running now | `in-flight / total-capacity`. Left: how many leases (agent turns, graph runs, harness ops, trigger fires) are running right now. Right: the sum of every active worker's parallel slot count. |
| Scheduler | Whether the claim engine is polling and ready. Shows time since the last claim cycle. |

The **Running now** tile turns amber when utilisation exceeds 80 percent. At 100 percent the pool is saturated and new work queues until a slot frees up.

## The worker table

Each row in the table represents one worker process. Columns:

- **ID** -- the worker's identifier.
- **Host / PID** -- the hostname and process id of the worker process.
- **Status** -- `active` (green), `draining` (amber), or `dead` (red).
- **Capacity** -- a segmented bar showing in-flight slots vs. total capacity, plus the session count on that worker.
- **Last heartbeat** -- seconds since the worker last checked in. Turns red after 30 seconds; a persistent red heartbeat indicates a hung or crashed process.
- **Started** -- relative time when the process started.

Use the filter bar to search by worker ID or host, or click a status chip (all / active / draining / dead) to narrow the list.

```callout:warning
A worker whose heartbeat exceeds 30 seconds is not necessarily dead; it may be occupied by a very long-running tool call. Check the session count in the Capacity column before concluding the process is stuck. If the session count is zero and the heartbeat is stale, the process has likely crashed and the pool will restart it automatically.
```

## Draining a worker

Draining tells a worker to finish its current in-flight sessions and then exit cleanly without picking up new ones. Use this before a planned restart or scaling-down event.

1. Find the worker row you want to drain.
2. Click the **Drain** button (right side of the row). A confirmation modal lists the number of in-flight sessions that will finish before drain completes.
3. Click **Drain worker** to confirm. The worker status changes to `draining`.

A draining worker does not accept new session claims. Once all in-flight sessions complete the worker exits, and the scheduler stops seeing it. Drain is idempotent: calling it on an already-draining worker is a no-op.

## Scheduler status

The Scheduler tile shows whether the claim engine is alive. The claim engine is the background loop that matches queued work to free worker slots. If the scheduler tile turns red or shows a stale last-claim time, new sessions will queue but not start. Check worker logs for scheduler-related errors.

## Automate this

```ref:reference/api-workers-health
Worker list, drain endpoint, and the health check resource.
```

## See also

```ref:concepts/yielding-and-claims
How the claim engine assigns work to workers and what happens when a session yields or parks.
```
