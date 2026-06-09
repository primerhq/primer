---
slug: graphs
title: Graphs - multi-step agent orchestration
summary: How primer composes multiple agents into directed graphs with conditional routing, supersteps, and callable routers; how to author graphs and invoke them.
related: [agents, sessions, semantic-search]
mcp_tools:
  - system::list_graphs
  - system::get_graph
  - system::create_graph
  - system::update_graph
  - system::delete_graph
  - system::find_graphs
  - search::search_graphs
---

# Graphs - multi-step agent orchestration

## Overview

A **Graph** is a directed graph of nodes that run agents (and other
node types), with edges that route the output of one node into the
input of another. Author one with `system::create_graph`, discover
existing ones with `search::search_graphs`, and run one by creating
a session with `graph_id`. It's primer's answer to "I need to chain
three agents in a specific way, with conditional branches based on
the output of one of them." Where a single agent is a turn-loop with
one LLM, a graph is a higher-level orchestrator: each node runs
its own complete turn-loop, and the graph chooses which node to
run next.

Use a graph when you need several agents chained with conditional
routing between them; not when one agent with one toolset does the
whole job (use a single [agent](agents.md) via
`system::create_agent`).

Graphs run to completion in one invocation. There's no mid-graph
pause in v1 (yields inside an agent node still work, but they pause
the agent within that node, not the graph topology). So a graph is
right for "deterministic multi-step work" - extract → analyse →
write report - not for "wait for a human between steps." For that,
use chats or sessions that internally compose multiple agents.

The execution model is **Pregel-style supersteps**. At each
superstep: a ready set of nodes runs in parallel. When each finishes,
edges out of those nodes are evaluated; nodes that become "ready"
(all required inputs present) join the next superstep's ready set.
The graph terminates when no ready nodes remain or when it hits a
terminal sink.

## Mental model

A `Graph` row carries:
- `id`, `description` (embedded for `search::search_graphs`).
- `nodes` - list of `Node` configs. Each has `id`, `kind`, and
  kind-specific config.
- `edges` - list of `Edge` configs connecting nodes.
- `sink` - terminal node id; when reached, graph stops.

Node kinds:
- `agent` - run an Agent. Config: `agent_id`, `input_template`
  (Jinja2 rendering the graph context into the agent's input).
- `task` - a plain LLM prompt, no tools. Config: `llm`, `prompt`,
  optional `response_format`.
- `subgraph` - invoke another Graph as a node. Config: `graph_id`,
  optional `input_template`.
- `http` - fetch a URL. Config: `method`, `url`, `headers`, `body`.
- `callable_router` - dispatch to a registered Python callback via
  `RouterRegistry`. Used for custom logic that can't be expressed
  as templates.

Edge kinds:
- `static` - always followed if source node completes.
- `json_path` - followed conditionally based on a JSONPath
  predicate over the source node's `NodeOutput.parsed`. Used for
  "if the agent's structured output says X, go to node Y".
- `callable_router` - dispatched to a Python callback that decides
  the next node id. Most flexible; most opaque.

Node output:
- `NodeOutput.text` - what most consumers read. For agent/task
  nodes, the LLM's final assistant message. For http, the
  response body. For callable_router, whatever the function
  returns.
- `NodeOutput.parsed` - populated only when the node has a
  `response_format`. Holds the structured JSON.

The graph context (the variable bag templates render against)
accumulates: each node's output is added under
`context.nodes[<node_id>]`. A later node's `input_template` can
reference `{{ nodes.extract.parsed.entities }}` to pull from an
earlier node's structured output.

A graph runs inside a session (call it a "graph session") - there's
a row in storage for the graph invocation, the worker pool claims it,
and the per-superstep state persists so the graph can resume if the
worker dies mid-execution. Recovery is per-superstep; partial within
a superstep is not preserved.

## Lifecycle and states

Graph invocations have the same status enum as agent sessions:
`RUNNING | WAITING | PAUSED | ENDED`. Same transitions, same claim
mechanics. The graph executor inside the worker advances supersteps
within `RUNNING` until a sink or no-ready-nodes condition triggers
`ENDED`.

Cycles in graphs are allowed but require a `max_iterations` cap on
the cyclic loop to prevent infinite supersteps. If a node revisits
without a cap, the graph errors out.

Subgraph nodes use a **stored reference** model. When a subgraph
node fires, it looks up the referenced Graph by id from storage -
so the subgraph definition is whatever's in storage at execution
time, not the version that was current at graph create time. Edits
to subgraphs hot-apply.

## MCP tools

### CRUD (system toolset)

- `system::list_graphs` - paginated.
- `system::get_graph` - fetch the full row.
- `system::create_graph` - body: optional `id`, `description`,
  `nodes`, `edges`, `sink`. Omit `id` and the server assigns
  `graph-<hex>` (e.g. `graph-7b2e44a1c0de`); supply one to use it
  verbatim. Immutable after creation.
- `system::update_graph` - partial update.
- `system::delete_graph` - cascade-blocked if any subgraph node
  references it.
- `system::find_graphs` - predicate query.

### Discovery (search toolset)

- `search::search_graphs` - semantic search over graph description
  + node ids. Useful when you know what you want to accomplish but
  not whether a graph exists for it.

To run a graph, create a session whose subject is the graph: call
`workspaces::create_workspace_session` with a graph binding
(`binding: {"kind": "graph", "graph_id": ...}`) instead of an agent
binding, and pass any input via `graph_input`.

## Workflows

### Workflow 1 - build a two-step extract-then-summarise graph

**Goal.** Wire up an extraction agent feeding a summarisation
agent.

1. Confirm both agents exist:

```json
{
  "tool": "search::search_agents",
  "arguments": {"query": "extract entities", "top_k": 3}
}
```

```json
{
  "tool": "search::search_agents",
  "arguments": {"query": "summarise document", "top_k": 3}
}
```

2. Create the graph:

```json
{
  "tool": "system::create_graph",
  "arguments": {
    "id": "extract-then-summarise",
    "description": "Extract entities from a document, then summarise around the extracted entities.",
    "nodes": [
      {
        "id": "extract",
        "kind": "agent",
        "config": {
          "agent_id": "extract-entities",
          "input_template": "Extract from: {{ input.text }}"
        }
      },
      {
        "id": "summarise",
        "kind": "agent",
        "config": {
          "agent_id": "summarise-document",
          "input_template": "Summarise around these entities: {{ nodes.extract.parsed.entities }}\\n\\nFull text:\\n{{ input.text }}"
        }
      }
    ],
    "edges": [
      {"kind": "static", "from": "extract", "to": "summarise"}
    ],
    "sink": "summarise"
  }
}
```

3. Run it via a session in a workspace:

```json
{
  "tool": "workspaces::create_workspace_session",
  "arguments": {
    "workspace_id": "ws-pipelines",
    "binding": {"kind": "graph", "graph_id": "extract-then-summarise"},
    "graph_input": {"text": "<input text or path here>"},
    "auto_start": true
  }
}
```

Response threads the session `id` and a `status` of `running`:
```json
{"id": "ses_3c1d", "status": "running"}
```

Poll `workspaces::get_workspace_session` with `{"workspace_id": "ws-pipelines", "session_id": "ses_3c1d"}` until `status` is `ended`;
the final node's output lands in the session's workspace.

### Workflow 2 - conditional routing based on an agent's structured output

**Goal.** A triage agent decides between three follow-up agents
based on its classification result.

The triage agent's `response_format`:
```json
{"type": "object", "properties": {"category": {"enum": ["bug", "feature", "support"]}}}
```

Graph definition (abbreviated):
```json
{
  "id": "triage-and-route",
  "description": "Triage an inbound message into category-specific agent.",
  "nodes": [
    {"id": "triage", "kind": "agent", "config": {"agent_id": "triage", "input_template": "{{ input.text }}"}},
    {"id": "handle_bug", "kind": "agent", "config": {"agent_id": "bug-handler", "input_template": "{{ input.text }}"}},
    {"id": "handle_feature", "kind": "agent", "config": {"agent_id": "feature-handler", "input_template": "{{ input.text }}"}},
    {"id": "handle_support", "kind": "agent", "config": {"agent_id": "support-handler", "input_template": "{{ input.text }}"}}
  ],
  "edges": [
    {"kind": "json_path", "from": "triage", "to": "handle_bug", "predicate": "$.category == 'bug'"},
    {"kind": "json_path", "from": "triage", "to": "handle_feature", "predicate": "$.category == 'feature'"},
    {"kind": "json_path", "from": "triage", "to": "handle_support", "predicate": "$.category == 'support'"}
  ],
  "sink": null
}
```

Only one of the three handler nodes runs per invocation, based on
the triage's category output.

## Gotchas

- **Graphs run to completion in one invocation.** There's no
  mid-graph yield in v1. Yielding tools inside an agent node still
  work but pause the agent within that node - the graph's
  superstep waits for the agent to finish before evaluating edges.
- **`json_path` edges read `NodeOutput.parsed`, not `text`.** The
  source node must have a `response_format` for the parsed field
  to be populated. Forgetting this is a frequent bug.
- **Cycles need `max_iterations` caps.** Without one, the
  executor errors on second visit. With one, the cap is checked
  per-node-per-graph-execution.
- **Subgraph references resolve at execution time.** Editing a
  subgraph mid-flight: subsequent invocations see the edit.
  Don't assume version pinning.
- **Tool outputs go into `text`, not `parsed`.** Even if the
  agent's last tool call returned JSON, the text field is what
  the next node's template renders. To make structured output
  flow, set `response_format` on the agent and pull from
  `nodes.<id>.parsed`.
- **Callable router nodes are opaque.** They dispatch to Python
  callbacks registered at app startup. Operators inspecting the
  graph see the callable id but not the logic. Use them sparingly
  for things that can't be expressed declaratively.
- **The graph session uses one workspace.** Every node in the
  graph runs in the same workspace. Files written by node A are
  visible to node B. Coordinate via filesystem.
- **Graphs share the same auto-compaction + tool approval +
  yielding mechanics as standalone agents** - those are
  per-agent-node concerns, not per-graph.

## Related

- [agents](agents.md) - each graph node typically wraps an agent.
- [sessions](sessions.md) - a graph runs inside a graph session.
- [semantic-search](semantic-search.md) - `search::search_graphs`
  is the discovery path.
