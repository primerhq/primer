---
title: "Graphs"
summary: "Build and run graphs: multi-step workflows of agents, tools and sub-graphs."
order: 50
---

A graph chains agents, tools and sub-graphs into a workflow with routing, loops and parallel branches. Start with [What a graph is](graphs/what-a-graph-is), write one with [Create and edit a graph](graphs/create-and-edit) and run it with [Run a graph](graphs/run-a-graph).

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`graphs__find`](reference/tools/graphs/find) | Find Graph with predicate. | user |
| [`graphs__get_access`](reference/tools/graphs/get_access) | Who Graph is shared with. | user |
| [`graphs__get_graph_node_turn_log`](reference/tools/graphs/get_graph_node_turn_log) | Read a single node's turn log within a graph run. | user |
| [`graphs__get_graph_run_node_states`](reference/tools/graphs/get_graph_run_node_states) | Per-node runtime status snapshot for a graph run. | user |
| [`graphs__get_graph_run_turn_log`](reference/tools/graphs/get_graph_run_turn_log) | Read graph-level turn log (superstep events). | user |
| [`graphs__graph_status`](reference/tools/graphs/graph_status) | Validate the graph's external references. | user |
| [`graphs__put_access`](reference/tools/graphs/put_access) | Change who Graph is shared with (owners only). | user |
