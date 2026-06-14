---
slug: graphs
title: Graphs - multi-step agent orchestration
summary: How primer composes agents, tools, and sub-graphs into directed graphs with Jinja-templated node inputs, conditional routing, fan-out/fan-in parallelism, and supersteps; how to author graphs and invoke them.
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

A **Graph** is a directed graph of nodes that run agents (and tools,
sub-graphs, and parallel fan-out subtrees), with edges that route
control from one node to the next. Each node builds its input by
rendering a **Jinja2 template** against the accumulated graph context,
so any node can pull data from any earlier node. Author one with
`system::create_graph`, discover existing ones with
`search::search_graphs`, and run one by creating a session with a
graph binding.

Where a single agent is a turn-loop with one LLM, a graph is a
higher-level orchestrator: each `agent` node runs its own complete
turn-loop, and the graph chooses which node runs next based on edges
and (optionally) the structured output of a node.

Use a graph when you need several steps chained with conditional
routing or parallelism between them; not when one agent with one
toolset does the whole job (use a single [agent](agents.md)).

Graphs run to completion in one invocation. There is no mid-graph
pause in v1 (yielding tools inside an agent node still work, but they
pause the agent within that node, not the graph topology). So a graph
is right for deterministic multi-step work (extract -> analyse ->
write report), not for waiting for a human between steps. For that,
use chats or sessions.

The execution model is **Pregel-style supersteps**. At each superstep
a ready set of nodes runs in parallel; when each finishes, edges out
of it are evaluated and newly-ready nodes join the next superstep. The
graph terminates when an `end` node fires or no ready nodes remain.

## Mental model

A `Graph` row carries:
- `id`, `description` (embedded for `search::search_graphs`).
- `nodes` - list of node configs (a discriminated union on `kind`).
- `edges` - list of edge configs connecting nodes (static or
  conditional).
- `max_iterations` - hard cap on supersteps. Required for any graph
  with a cycle or a callable router; optional otherwise.

There is no `sink` field: termination is structural. Every graph MUST
have **exactly one `begin` node** and **at least one `end` node**; the
graph stops when an `end` fires.

### Node kinds

Each node has `id`, `kind`, and kind-specific fields **at the top
level** (there is no `config` wrapper).

- **`begin`** - the single entry node. No template, no LLM. Optional
  `input_schema` (JSON Schema 2020-12): when set, the graph's input is
  validated at session-create (422 on mismatch). Begin has no incoming
  edges.
- **`agent`** - runs a stored Agent. Fields: `agent_id`,
  `input_template` (Jinja, rendered to the user-role text appended to
  the agent's history before its turn), optional `response_format`
  (JSON Schema; when set the agent produces structured output and
  `NodeOutput.parsed` is populated - a node with `response_format` is a
  data-shaping turn and is offered **no tools**, so it cannot call tools
  and emit structured output in the same turn), optional `input_schema`
  (soft warn-only validation of the rendered input).
- **`tool_call`** - invokes a tool directly, no LLM. Fields: `tool_id`
  (scoped `toolset_id__bare_name` - workspace tools use the `workspace`
  scope, e.g. `workspace__write` / `workspace__exec`; internal toolsets
  use their own, e.g. `web__web_search`, `system__call_tool`),
  `arguments` (an object whose
  **string leaves are each Jinja-rendered** against the context;
  non-string leaves pass through), or `arguments_template` (a
  full-JSON Jinja template that shadows `arguments` for dynamic
  shapes), optional `output_schema`.
- **`graph`** - delegates to another stored Graph (sub-graph). Fields:
  `graph_id`, `input_template` (rendered to the sub-graph's input).
  The reference resolves at execution time, so edits to the sub-graph
  hot-apply.
- **`fan_out`** - spawns parallel downstream executions. Field:
  `specs` (one or more). Each spec is `broadcast` (N copies of one
  target via `target_node_id` + `count`), `tee` (run each of
  `target_node_ids` once), or `map` (read a list from
  `source_node_id` + `source_path` and run one instance of
  `target_node_id` per item). `on_failure` is `fail_fast` (default),
  `drain_then_fail`, or `collect`.
- **`fan_in`** - waits for ALL incoming branches, then aggregates.
  Fields: `aggregate_template` (Jinja, result becomes
  `NodeOutput.text`), optional `output_schema`. For a fan-out source,
  "all incoming produced output" means every synthesized instance.
- **`end`** - a terminal sink. Fields: `output_template` (Jinja over
  the context, rendered to the graph's final output; empty template
  terminates with no payload), optional `output_schema` (rendered
  output must parse as conforming JSON, else
  `ended_detail='end_output_invalid'`). End nodes have no outgoing
  edges.

### Edge kinds

- **`static`** - `{kind, from_node, to_node}`. Always fires when
  `from_node` completes.
- **`conditional`** - `{kind, from_node, router}`. The router resolves
  the destination. Two router kinds:
  - **`json_path`** - `{kind:"json_path", branches:[...], default_to}`.
    Each branch is `{conditions:[{path, op, value}], to_node}`; the
    first branch whose conditions ALL hold (AND) fires. `op` is one of
    `eq | ne | gt | gte | lt | lte | in | not_in | exists`. `path` is
    a dotted/bracket path (`a.b[2].c`) into the source node's
    `NodeOutput.parsed`, so the source must have a `response_format`.
    An empty `conditions` list matches everything (catch-all). When no
    branch matches, `default_to` fires; `null` ends the graph as
    `failed`.
  - **`callable`** - `{kind:"callable", callable_id}`. Dispatches to a
    Python callback registered in the executor's `RouterRegistry`
    (`(context, source) -> node_id`). Most flexible; opaque to
    inspectors. Used for logic that cannot be expressed declaratively.

### Node output

When a node finishes, its result lands in `context.nodes[<node_id>]`
as a `NodeOutput`:
- `text` - the node's final assistant text (for an agent), the tool
  result, or the rendered template (for tool/fan_in/end). What most
  consumers read.
- `parsed` - the structured JSON, populated **only** when the node had
  a `response_format`.
- `history` - the node's full message history.
- `iteration` - the graph iteration that produced it.
- `error` / `ended_detail` - set only for a node that failed inside a
  fan-out subtree configured with `on_failure='collect'`.

Fan-out targets surface as a **list** at `nodes[target]` (the
aggregator), with individual instances at `nodes['target[i]']`.

## The input templating engine

Node inputs are rendered by a **sandboxed Jinja2** environment
(`primer/graph/template.py`): no dunder/`__import__` access, and
**`StrictUndefined`** - a missing variable or attribute raises an
error (surfaced as a bad-request), it does NOT silently render empty.
So a typo in a placeholder fails loudly at run time.

Every template (`agent`/`graph` `input_template`, `end`
`output_template`, `fan_in` `aggregate_template`, and each string leaf
in a `tool_call` `arguments`) renders against the **graph context**,
which exposes exactly three top-level variables:

| Variable | Meaning |
|----------|---------|
| `initial_input` | The graph's input - whatever you passed as `graph_input` at session-create. Its type is **whatever you passed** (a dict, a string, or a `list[Message]`). NOT named `input`. |
| `iteration` | The current superstep counter (int). The `begin` node runs at iteration 0, so the **first agent node runs at iteration 1**, not 0. Do not use `iteration == 0` to detect a node's first run in a loop - test `nodes.<prev> is defined` instead (see Workflow 4). |
| `nodes` | A dict keyed by node id; values are `NodeOutput` (or `list[NodeOutput]` for fan-out targets, including `tee`). Holds only nodes that have **already run** this far - referencing a node that has not run yet (e.g. an un-taken conditional branch) raises under `StrictUndefined`, so guard with `{% if nodes.<id> is defined %}`. Access via attribute syntax: `nodes.extract.text`, `nodes.extract.parsed.entities`. |

Inside a fan-out `map`/`broadcast` instance, two more variables are in
scope for that instance's template: `fanout_index` (int) and
`fanout_item` (the per-instance item).

The **default** `input_template` (when omitted) is
`{% for m in initial_input %}{{ m.parts[0].text }}\n{% endfor %}` - it
assumes `initial_input` is a `list[Message]`. If you pass a dict or
string as `graph_input`, write your own template (e.g.
`{{ initial_input.text }}` or `{{ initial_input }}`).

Common placeholder patterns:
- `{{ initial_input.text }}` - read a field of a dict input.
- `{{ nodes.extract.text }}` - a prior node's raw text.
- `{{ nodes.triage.parsed.category }}` - a prior node's structured
  field (requires `response_format` on that node).
- `{% for r in nodes.scatter %}{{ r.text }}\n{% endfor %}` - iterate a
  fan-out target's aggregated list.
- `{{ fanout_item }}` - the current item inside a `map` instance.

`initial_input` is a constant available to **every** node's template (it
never mutates), so any node can read the original run input regardless of
where it sits in the graph.

### Structured input and per-node instructions

Because `initial_input` is whatever you passed as `graph_input`, you can send
a structured object and have each node pull only its slice - including
per-node instructions keyed by node id:

```json
{
  "document": "<text>",
  "instructions": {"extract": "Company names only.", "summarise": "Three bullets, formal tone."}
}
```

```text
extract node    input_template: "{{ initial_input.instructions.extract }}\n\n{{ initial_input.document }}"
summarise node  input_template: "{{ initial_input.instructions.summarise }}\n\n{{ nodes.extract.text }}"
```

This re-parameterises one reusable graph per run without editing its
definition. Guard optional keys with the `default` filter
(`{{ initial_input.instructions.extract | default('') }}`) since
`StrictUndefined` makes a missing key a hard error, and pin the shape with
the `begin` node's `input_schema` if you want a 422 on bad input.

## What you can build

Because a node template can read any ancestor, pull structured
fields, and use full Jinja, graphs go well beyond linear chains:

- **Linear pipeline** - extract -> analyse -> write; each node pulls
  `nodes.<prev>.text`.
- **Data-driven branching / state machine** - an agent with
  `response_format` plus a `conditional` json_path edge routing on
  `parsed`. The structured output is the state.
- **Scatter-gather / map-reduce** - `fan_out` map over a list ->
  parallel agent per item (`{{ fanout_item }}`) -> `fan_in` reduce
  over `nodes.<target>`.
- **Best-of-N / judge panel** - `fan_out` broadcast N copies ->
  `fan_in` (or a judge agent) selects/synthesises.
- **Multi-lens analysis** - `fan_out` tee the same input to several
  different agents -> `fan_in` merge.
- **Iterative refinement loop** - a cycle (writer -> critic -> writer)
  with `max_iterations`, the writer reading `nodes.critic.text` and
  `iteration`.
- **Deterministic tool steps** - `tool_call` nodes with templated
  `arguments` interleaved with agents (e.g. search with
  `{"query": "{{ nodes.plan.parsed.topic }}"}`).
- **Hierarchical composition** - `graph` nodes delegating to reusable
  sub-graphs.
- **Multi-source join** - one node fusing outputs from several
  ancestors in its template.
- **Validation gates** - `output_schema` on end/tool_call/fan_in to
  guarantee a conforming output.

Nodes also share one workspace, so they can coordinate via files (one
node writes, a later node reads) in addition to template data-flow.

## Lifecycle and states

Graph invocations use the same status enum as agent sessions:
`RUNNING | WAITING | PAUSED | ENDED`, with the same claim mechanics.
The executor advances supersteps within `RUNNING` until an `end` node
or a no-ready-nodes condition triggers `ENDED`. Per-superstep state
persists so the graph resumes if the worker dies mid-execution;
recovery is per-superstep (partial within a superstep is not
preserved).

Cycles are allowed but require `max_iterations`; a callable router
also requires it (its targets are not statically known). Without the
cap a loopable graph is rejected at create time.

## MCP tools

### CRUD (system toolset)

- `system::list_graphs` - paginated.
- `system::get_graph` - fetch the full row.
- `system::create_graph` - body `{"entity": {...}}` with optional
  `id`, `description`, `nodes`, `edges`, `max_iterations`. Omit `id`
  and the server assigns `graph-<hex>`; supply one to use it verbatim.
  Immutable after creation.
- `system::update_graph` - replace the row.
- `system::delete_graph` - cascade-blocked if any `graph` node
  references it.
- `system::find_graphs` - predicate query.

### Discovery (search toolset)

- `search::search_graphs` - semantic search over graph description +
  node ids.

To run a graph, create a session whose subject is the graph: call
`workspaces::create_workspace_session` with a graph binding
(`binding: {"kind": "graph", "graph_id": ...}`) and pass input via
`graph_input`.

## Workflows

### Workflow 1 - a two-step extract-then-summarise graph

**Goal.** Wire an extraction agent feeding a summarisation agent.
The extraction agent has a `response_format` so its entities flow as
structured `parsed`.

```json
{
  "tool": "system::create_graph",
  "arguments": {
    "entity": {
      "id": "extract-then-summarise",
      "description": "Extract entities from a document, then summarise around them.",
      "nodes": [
        {"id": "start", "kind": "begin"},
        {
          "id": "extract",
          "kind": "agent",
          "agent_id": "extract-entities",
          "input_template": "Extract entities from:\n{{ initial_input.text }}",
          "response_format": {"type": "object", "properties": {"entities": {"type": "array", "items": {"type": "string"}}}}
        },
        {
          "id": "summarise",
          "kind": "agent",
          "agent_id": "summarise-document",
          "input_template": "Summarise around these entities: {{ nodes.extract.parsed.entities }}\n\nFull text:\n{{ initial_input.text }}"
        },
        {"id": "done", "kind": "end", "output_template": "{{ nodes.summarise.text }}"}
      ],
      "edges": [
        {"kind": "static", "from_node": "start", "to_node": "extract"},
        {"kind": "static", "from_node": "extract", "to_node": "summarise"},
        {"kind": "static", "from_node": "summarise", "to_node": "done"}
      ]
    }
  }
}
```

Run it in a workspace:

```json
{
  "tool": "workspaces::create_workspace_session",
  "arguments": {
    "workspace_id": "ws-pipelines",
    "binding": {"kind": "graph", "graph_id": "extract-then-summarise"},
    "graph_input": {"text": "<input text here>"},
    "auto_start": true
  }
}
```

Poll `workspaces::get_workspace_session` until `status` is `ended`;
the `end` node's rendered `output_template` is the graph's output.

### Workflow 2 - conditional routing on an agent's structured output

**Goal.** A triage agent routes to one of three handlers based on its
classification. The triage agent's `response_format`:
`{"type":"object","properties":{"category":{"enum":["bug","feature","support"]}}}`.

```json
{
  "entity": {
    "id": "triage-and-route",
    "description": "Triage an inbound message into a category-specific handler.",
    "nodes": [
      {"id": "start", "kind": "begin"},
      {"id": "triage", "kind": "agent", "agent_id": "triage", "input_template": "{{ initial_input.text }}", "response_format": {"type":"object","properties":{"category":{"enum":["bug","feature","support"]}}}},
      {"id": "handle_bug", "kind": "agent", "agent_id": "bug-handler", "input_template": "{{ initial_input.text }}"},
      {"id": "handle_feature", "kind": "agent", "agent_id": "feature-handler", "input_template": "{{ initial_input.text }}"},
      {"id": "handle_support", "kind": "agent", "agent_id": "support-handler", "input_template": "{{ initial_input.text }}"},
      {"id": "done", "kind": "end", "output_template": "{% if nodes.handle_bug is defined %}{{ nodes.handle_bug.text }}{% endif %}{% if nodes.handle_feature is defined %}{{ nodes.handle_feature.text }}{% endif %}{% if nodes.handle_support is defined %}{{ nodes.handle_support.text }}{% endif %}"}
    ],
    "edges": [
      {"kind": "static", "from_node": "start", "to_node": "triage"},
      {"kind": "conditional", "from_node": "triage", "router": {
        "kind": "json_path",
        "branches": [
          {"conditions": [{"path": "category", "op": "eq", "value": "bug"}], "to_node": "handle_bug"},
          {"conditions": [{"path": "category", "op": "eq", "value": "feature"}], "to_node": "handle_feature"},
          {"conditions": [{"path": "category", "op": "eq", "value": "support"}], "to_node": "handle_support"}
        ],
        "default_to": "handle_support"
      }},
      {"kind": "static", "from_node": "handle_bug", "to_node": "done"},
      {"kind": "static", "from_node": "handle_feature", "to_node": "done"},
      {"kind": "static", "from_node": "handle_support", "to_node": "done"}
    ]
  }
}
```

Only one handler runs per invocation, chosen by the triage category.
Because the other two handlers never run, the `end` node guards each
with `{% if nodes.<id> is defined %}` - referencing an un-taken branch
directly would raise under `StrictUndefined`.

### Workflow 3 - scatter-gather (map then reduce)

**Goal.** Classify every item of a list in parallel, then aggregate.
A `map` fan-out spawns one `classify` instance per list item; the
instance reads its item via `{{ fanout_item }}`. Note the wiring: the
`fan_out` node has **no outgoing edge** (the spec wires `scatter ->
classify`); each `classify` instance follows `classify`'s outgoing
edge into the `fan_in`, which waits for all instances.

```json
{
  "entity": {
    "id": "map-reduce-reviews",
    "description": "Classify each review in parallel, then aggregate.",
    "nodes": [
      {"id": "start", "kind": "begin"},
      {"id": "split", "kind": "agent", "agent_id": "list-emitter",
       "input_template": "Return the reviews as a JSON array of strings:\n{{ initial_input.reviews }}",
       "response_format": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}}}},
      {"id": "scatter", "kind": "fan_out",
       "specs": [{"kind": "map", "target_node_id": "classify", "source_node_id": "split", "source_path": "items"}]},
      {"id": "classify", "kind": "agent", "agent_id": "sentiment",
       "input_template": "Classify the sentiment (positive/negative/neutral) of review #{{ fanout_index }}:\n{{ fanout_item }}"},
      {"id": "gather", "kind": "fan_in",
       "aggregate_template": "{% for r in nodes.classify %}- {{ r.text }}\n{% endfor %}"},
      {"id": "done", "kind": "end", "output_template": "{{ nodes.gather.text }}"}
    ],
    "edges": [
      {"kind": "static", "from_node": "start", "to_node": "split"},
      {"kind": "static", "from_node": "split", "to_node": "scatter"},
      {"kind": "static", "from_node": "classify", "to_node": "gather"},
      {"kind": "static", "from_node": "gather", "to_node": "done"}
    ]
  }
}
```

### Workflow 4 - iterative refinement loop

**Goal.** Draft, critique, and revise until the critic says it is good
enough (or `max_iterations` is hit). The cycle `write -> critique ->
write` requires `max_iterations`. The writer detects its first run by
testing whether the critic has run yet (`nodes.critique is defined`) -
**not** `iteration == 0`, since the first `write` runs at iteration 1
(begin is iteration 0). The critic's structured `done` flag drives a
`json_path` router that either ends or loops back.

```json
{
  "entity": {
    "id": "draft-and-refine",
    "description": "Draft, critique, revise up to a cap.",
    "max_iterations": 6,
    "nodes": [
      {"id": "start", "kind": "begin"},
      {"id": "write", "kind": "agent", "agent_id": "writer",
       "input_template": "{% if nodes.critique is defined %}Revise your draft using this critique:\n{{ nodes.critique.text }}\n\nPrevious draft:\n{{ nodes.write.text }}{% else %}Write a first draft about: {{ initial_input.topic }}{% endif %}"},
      {"id": "critique", "kind": "agent", "agent_id": "critic",
       "input_template": "Critique this draft; set done=true only if it needs no further work:\n{{ nodes.write.text }}",
       "response_format": {"type": "object", "properties": {"done": {"type": "boolean"}, "notes": {"type": "string"}}}},
      {"id": "done", "kind": "end", "output_template": "{{ nodes.write.text }}"}
    ],
    "edges": [
      {"kind": "static", "from_node": "start", "to_node": "write"},
      {"kind": "static", "from_node": "write", "to_node": "critique"},
      {"kind": "conditional", "from_node": "critique", "router": {
        "kind": "json_path",
        "branches": [{"conditions": [{"path": "done", "op": "eq", "value": true}], "to_node": "done"}],
        "default_to": "write"
      }}
    ]
  }
}
```

### Workflow 5 - tool-augmented pipeline

**Goal.** An agent plans a query, a `tool_call` node runs the search
directly (no agent turn), and a writer answers from the results. The
tool's `arguments` are templated per leaf: the string `query` is
rendered from the planner's structured output; the non-string
`max_results` passes through unchanged.

```json
{
  "entity": {
    "id": "research-and-write",
    "description": "Plan a query, search the web, write from results.",
    "nodes": [
      {"id": "start", "kind": "begin"},
      {"id": "plan", "kind": "agent", "agent_id": "planner",
       "input_template": "Pick one web search query that answers: {{ initial_input.question }}",
       "response_format": {"type": "object", "properties": {"query": {"type": "string"}}}},
      {"id": "search", "kind": "tool_call", "tool_id": "web__web_search",
       "arguments": {"query": "{{ nodes.plan.parsed.query }}", "max_results": 5}},
      {"id": "write", "kind": "agent", "agent_id": "writer",
       "input_template": "Answer using these results:\n{{ nodes.search.text }}\n\nQuestion: {{ initial_input.question }}"},
      {"id": "done", "kind": "end", "output_template": "{{ nodes.write.text }}"}
    ],
    "edges": [
      {"kind": "static", "from_node": "start", "to_node": "plan"},
      {"kind": "static", "from_node": "plan", "to_node": "search"},
      {"kind": "static", "from_node": "search", "to_node": "write"},
      {"kind": "static", "from_node": "write", "to_node": "done"}
    ]
  }
}
```

### Workflow 6 - best-of-N with a judge

**Goal.** Run N independent attempts in parallel (`broadcast`), then a
judge agent picks the best from the aggregated list. The `fan_in`
hands the candidates to the judge via its `input_template` reading the
`{{ nodes.candidate }}` list.

```json
{
  "entity": {
    "id": "best-of-n",
    "description": "Generate N candidates, judge selects the best.",
    "nodes": [
      {"id": "start", "kind": "begin"},
      {"id": "spread", "kind": "fan_out", "specs": [{"kind": "broadcast", "target_node_id": "candidate", "count": 3}]},
      {"id": "candidate", "kind": "agent", "agent_id": "solver",
       "input_template": "Attempt #{{ fanout_index }}. Solve:\n{{ initial_input.problem }}"},
      {"id": "judge", "kind": "fan_in",
       "aggregate_template": "Pick the best of these candidates and return only it:\n{% for c in nodes.candidate %}--- candidate {{ loop.index0 }} ---\n{{ c.text }}\n{% endfor %}"},
      {"id": "done", "kind": "end", "output_template": "{{ nodes.judge.text }}"}
    ],
    "edges": [
      {"kind": "static", "from_node": "start", "to_node": "spread"},
      {"kind": "static", "from_node": "candidate", "to_node": "judge"},
      {"kind": "static", "from_node": "judge", "to_node": "done"}
    ]
  }
}
```

(For a judge that itself runs an LLM, route the `fan_in` output into a
following `agent` node instead of doing the selection inside the
`aggregate_template`.)

## Gotchas

- **The template variable is `initial_input`, not `input`.** With
  `StrictUndefined`, `{{ input.text }}` raises at render time.
- **Exactly one `begin`, at least one `end`.** Begin has no incoming
  edges; end nodes have no outgoing edges; every end must be reachable
  from begin, or create fails.
- **json_path routers read `NodeOutput.parsed`, not `text`.** The
  source node must set `response_format` or `parsed` is `null` and
  every condition is False. A missing `path` makes every operator
  return False (use `op: "exists"` to test presence).
- **`nodes.<id>` holds only nodes that already ran.** Referencing an
  un-taken conditional branch (or any not-yet-run node) raises under
  `StrictUndefined`; guard with `{% if nodes.<id> is defined %}`. And
  `iteration` is the global superstep counter (begin is 0, the first
  agent node is 1), so detect a loop's first pass with
  `nodes.<prev> is defined`, not `iteration == 0`.
- **A fan-out target is a list in `nodes`.** After `broadcast`/`map`/`tee`,
  `nodes.<target>` is a `list[NodeOutput]` - iterate it
  (`{% for r in nodes.<target> %}`) or index it (`nodes.<target>[0].text`).
  This holds even for `tee`, where each named target runs once and
  becomes a single-element list.
- **Node fields are top-level, edges use `from_node`/`to_node`.**
  There is no `config` wrapper and no `from`/`to` shorthand.
- **Tool outputs land in `text`, not `parsed`.** To make an agent's
  structured output flow, set its `response_format` and read
  `nodes.<id>.parsed`.
- **Cycles and callable routers need `max_iterations`.** A loopable
  graph without the cap is rejected at create time.
- **Sub-graph references resolve at execution time.** Editing a
  sub-graph mid-flight: subsequent invocations see the edit.
- **Callable routers are opaque.** They dispatch to callbacks
  registered at app startup; inspectors see only the `callable_id`.
- **One workspace per graph session.** Every node runs in the same
  workspace; files written by node A are visible to node B.
- **Per-agent-node concerns.** Auto-compaction, tool approval, and
  yielding are per-agent-node, not per-graph.

## Related

- [agents](agents.md) - each agent node wraps a stored Agent.
- [sessions](sessions.md) - a graph runs inside a graph session.
- [semantic-search](semantic-search.md) - `search::search_graphs` is
  the discovery path.
