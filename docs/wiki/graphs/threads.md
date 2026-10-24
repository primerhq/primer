---
title: "Graph threads"
summary: "A graph thread records one run of a graph: its steps, node states and how it ended. Read and clean them up."
order: 4
tools: [system__list_graph_threads, system__get_graph_thread, system__create_graph_thread, system__update_graph_thread, system__delete_graph_thread, system__find_graph_threads]
related: [graphs/run-a-graph, sessions/observe-a-session]
---
## What it is
A graph thread is a stored row that records one run of a graph by the storage-backed graph runner: which graph ran, how many steps it took, the state of each node and how the run ended. The six `system` thread tools read, write and delete these rows.

Graph runs in sessions do not write threads. A graph session, and a graph started with `workspace_ext__invoke_graph`, keep their state in the workspace, under the session. To follow or inspect those runs, read the session ([run a graph](graphs/run-a-graph), [observe a session](sessions/observe-a-session)). The thread tools are for inspecting and cleaning up thread rows: on a stock install the table holds only rows that a tool or code wrote there.

## Concepts
- A thread row holds `id`, `graph_id`, `title`, `created_at`, `last_activity_at`, `iteration`, `node_states`, `status`, `ended_reason` and `ended_detail`.
- `iteration` is the last step the run reached.
- `node_states` maps each node id to its state: `status` (`pending`, `running`, `waiting`, `ended` or `failed`), `last_run_iteration`, `last_run_at` and `error`. A credential in an `error` text is masked.
- `status` is the run's aggregate state: `created`, `running`, `waiting`, `paused` or `ended`.
- `ended_reason` is set once the status is `ended`: `completed`, `failed`, `cancelled` or `max_iterations_exceeded`. `ended_detail` carries the failure code of a failed run (`routing_failed`, `template_error`, `end_output_invalid` and the others on [what a graph is](graphs/what-a-graph-is)).
- Each node's message history is kept in separate per-node rows. No tool serves them, and deleting a thread does not delete them.
- Every thread tool is open to the `user` role and ships ungated.

## How to
1. List or find the threads you care about, by `graph_id` or by `status`.
2. Get one to read its `node_states` and how it ended.
3. Delete the rows you no longer need.
4. Write a row (create or update) only when your own process records runs here: an update replaces the whole row.

## Tool calls
### Find a graph's failed runs
`system__find_graph_threads` filters rows by field; this one keeps the runs of one graph that ended `failed`.

```json
{"toolset_id": "system", "tool_name": "find_graph_threads", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "predicate", "left": {"kind": "field", "name": "graph_id"}, "op": "=", "right": {"kind": "value", "value": "triage-and-route"}}, "op": "and", "right": {"kind": "predicate", "left": {"kind": "field", "name": "ended_reason"}, "op": "=", "right": {"kind": "value", "value": "failed"}}}}}
```

Reference: [`system__find_graph_threads`](reference/tools/system/find_graph_threads)

### List threads
`system__list_graph_threads` pages through every row.

```json
{"toolset_id": "system", "tool_name": "list_graph_threads", "arguments": {"limit": 20}}
```

Reference: [`system__list_graph_threads`](reference/tools/system/list_graph_threads)

### Get a thread
`system__get_graph_thread` returns one row whole, with its `node_states`.

```json
{"toolset_id": "system", "tool_name": "get_graph_thread", "arguments": {"id": "gt-5b1e09c4d7a2"}}
```

Reference: [`system__get_graph_thread`](reference/tools/system/get_graph_thread)

### Delete a thread
`system__delete_graph_thread` removes the row.

```json
{"toolset_id": "system", "tool_name": "delete_graph_thread", "arguments": {"id": "gt-5b1e09c4d7a2"}}
```

Reference: [`system__delete_graph_thread`](reference/tools/system/delete_graph_thread)

### Create a thread
`system__create_graph_thread` stores a new row. The `id` is required: a thread gets no generated id.

```json
{"toolset_id": "system", "tool_name": "create_graph_thread", "arguments": {"entity": {"id": "gt-nightly-2026-10-24", "graph_id": "triage-and-route", "title": "Nightly triage", "created_at": "2026-10-24T02:00:00Z", "last_activity_at": "2026-10-24T02:00:00Z"}}}
```

Reference: [`system__create_graph_thread`](reference/tools/system/create_graph_thread)

### Update a thread
`system__update_graph_thread` replaces the whole row: send every field you want to keep.

```json
{"toolset_id": "system", "tool_name": "update_graph_thread", "arguments": {"id": "gt-nightly-2026-10-24", "entity": {"id": "gt-nightly-2026-10-24", "graph_id": "triage-and-route", "title": "Nightly triage", "created_at": "2026-10-24T02:00:00Z", "last_activity_at": "2026-10-24T02:04:10Z", "iteration": 3, "status": "ended", "ended_reason": "completed"}}}
```

Reference: [`system__update_graph_thread`](reference/tools/system/update_graph_thread)

## Gotchas
- An empty list is the normal answer: session runs never write here. Look for a session-run graph in the session's transcript, not in the threads.
- A create or update does not check that `graph_id` names a graph, and deleting a graph leaves its threads in place.
- `max_iterations_exceeded` is an allowed `ended_reason`, but the runner records a capped run as `failed` with `ended_detail` `max_iterations_exceeded`. Filter on both when you look for capped runs.
- An update replaces the row: a field you leave out is reset to its default (`node_states` empty, `iteration` 0).

## Related
[run a graph](graphs/run-a-graph), [observe a session](sessions/observe-a-session).
