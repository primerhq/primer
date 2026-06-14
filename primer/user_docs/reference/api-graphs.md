---
slug: api-graphs
title: Graphs API
section: reference
summary: REST endpoints to create, list, update, delete, and validate directed agent graphs.
---

A graph is a directed network of typed nodes connected by static or conditional edges. The executor walks nodes in Pregel-style supersteps; each node produces output that downstream nodes consume via Jinja2 templates. Every graph must have exactly one `begin` node and at least one `end` node.

```ref:features/agents
Agents that graph nodes reference.
```

```ref:features/graphs
Build and run graphs in the console.
```

## Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/v1/graphs` | List graphs (offset or cursor pagination) |
| POST | `/v1/graphs` | Create a graph |
| GET | `/v1/graphs/{id}` | Get graph by id |
| PUT | `/v1/graphs/{id}` | Replace (full update) a graph |
| DELETE | `/v1/graphs/{id}` | Delete a graph |
| POST | `/v1/graphs/find` | Filter graphs by predicate |
| GET | `/v1/graphs/{id}/status` | Validate the graph's external references |
| GET | `/v1/graphs/{id}/runs/{run_id}/turn_log` | Read the graph-level turn log for a run |
| GET | `/v1/graphs/{id}/runs/{run_id}/nodes/{node_id}/turn_log` | Read a single node's turn log |

## Graph object

```json
{
  "id": "research-pipeline",
  "description": "Research then summarize.",
  "nodes": [
    {"kind": "begin", "id": "start"},
    {"kind": "agent", "id": "researcher", "agent_id": "research-agent"},
    {"kind": "agent", "id": "summarizer", "agent_id": "summary-agent",
     "input_template": "Summarize: {{ nodes.researcher.text }}"},
    {"kind": "end", "id": "done", "output_template": "{{ nodes.summarizer.text }}"}
  ],
  "edges": [
    {"kind": "static", "from_node": "start", "to_node": "researcher"},
    {"kind": "static", "from_node": "researcher", "to_node": "summarizer"},
    {"kind": "static", "from_node": "summarizer", "to_node": "done"}
  ],
  "max_iterations": null,
  "harness_id": null
}
```

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `id` | no | string | Identifier (case-sensitive). If omitted, the server assigns a type-prefixed id (e.g. `graph-7b2e44a1c0de`). Immutable after creation |
| `description` | yes | string | Human-readable description |
| `nodes` | yes | GraphNode[] | At least one node; must include exactly one `begin` and at least one `end` |
| `edges` | no | GraphEdge[] | Static or conditional edges; default empty list |
| `max_iterations` | no | integer or null | Hard cap on supersteps. Required for cyclic graphs to prevent unbounded loops |
| `harness_id` | no | string or null | Set by harness management; mutation via CRUD returns 409 when set |

## Node kinds

| Kind | Description |
|------|-------------|
| `begin` | Entry point. Exactly one required per graph. No incoming edges allowed |
| `end` | Sink node. At least one required. Renders `output_template` to produce graph output. No outgoing edges allowed |
| `agent` | Runs a stored Agent via `agent_id`. Accepts `input_template`, `response_format`, `description`, `input_schema` |
| `graph` | Delegates to a stored sub-graph via `graph_id`. Accepts `input_template`, `description` |
| `fan_out` | Dispatches parallel branches. Targets defined on `specs` (not in `edges`). Supports `broadcast`, `tee`, and `map` kinds |
| `fan_in` | Waits for all incoming branches then renders `aggregate_template`. Must have at least one incoming edge |
| `tool_call` | Calls a tool directly by `tool_id` (scoped form `toolset_id__bare_name`). Accepts `arguments` dict or `arguments_template` |

**begin node fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Within-graph unique node id |
| `input_schema` | no | JSON Schema 2020-12. When set, session create validates `graph_input` against it; failure returns 422 |

**end node fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Within-graph unique node id |
| `output_template` | no | Jinja2 template rendered over GraphContext when End fires. Empty means no output payload |
| `output_schema` | no | JSON Schema 2020-12. When set, rendered output must parse as valid JSON; failure ends with `ended_detail=end_output_invalid` |

**agent node fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Within-graph unique node id |
| `agent_id` | yes | Id of the stored Agent this node executes |
| `input_template` | no | Jinja2 template producing the user message passed to the agent. Default concatenates `initial_input` |
| `response_format` | no | JSON Schema forwarded to the agent. Populates `NodeOutput.parsed` when set |

**fan_out spec kinds:**

| Spec kind | Required fields | Description |
|-----------|----------------|-------------|
| `broadcast` | `target_node_id`, `count` | Spawns `count` synthesized instances of one target node |
| `tee` | `target_node_ids` | Runs each named target once with the fan-out's input |
| `map` | `target_node_id`, `source_node_id`, `source_path` | Parses a list from a source node's output and runs one instance per item |

## Node input templating

Every node that takes input builds it by rendering a **Jinja2 template** against the graph's accumulated state. This applies to an `agent`/`graph` node's `input_template`, an `end` node's `output_template`, a `fan_in` node's `aggregate_template`, and each string value inside a `tool_call` node's `arguments` (or its `arguments_template`).

The renderer is sandboxed and uses `StrictUndefined`: a reference to a missing variable or attribute raises a render error (returned as `400`), it does not silently produce an empty string. Guard optional fields with the `default` filter, e.g. `{{ initial_input.note | default('') }}`.

Templates see exactly three top-level variables, available to **every** node:

| Variable | Meaning |
|----------|---------|
| `initial_input` | The graph's run input: whatever you passed as `graph_input` when starting the session. Its type is whatever you sent (a dict, string, or list of messages). Referenced as `initial_input`, not `input`. |
| `iteration` | The current superstep counter (integer). `begin` runs at 0, so the **first agent node runs at iteration 1**. Do not detect a loop's first pass with `iteration == 0`; test `nodes.<prev> is defined` instead. |
| `nodes` | A map of **already-run** node outputs keyed by node id. Each value is a `NodeOutput` with `.text`, `.parsed` (set only when that node had a `response_format`), and `.history`. Referencing a not-yet-run node (e.g. an un-taken conditional branch) raises under `StrictUndefined` - guard with `{% if nodes.<id> is defined %}`. Fan-out targets (`broadcast`/`map`/`tee`) appear as a `list[NodeOutput]` at `nodes.<target>`; index or iterate it (`nodes.<target>[0].text`). |

Inside a fan-out `map`/`broadcast` instance, two more variables are in scope for that instance only: `fanout_index` (integer) and `fanout_item` (that instance's item).

### Structured input and per-node instructions

`initial_input` carries your `graph_input` verbatim, so you can send a structured object and have each node pull only what it needs:

```json
{
  "document": "<text>",
  "instructions": {
    "extract": "Extract company names only.",
    "summarise": "Three bullets, formal tone."
  }
}
```

```text
extract node    input_template: "{{ initial_input.instructions.extract }}\n\n{{ initial_input.document }}"
summarise node  input_template: "{{ initial_input.instructions.summarise }}\n\n{{ nodes.extract.text }}"
```

Keying per-node directives by node id in one structured input lets a single reusable graph be re-parameterised each run without editing its definition. Two practical notes:

- The default `input_template` (when omitted) assumes `initial_input` is a list of messages. If you send a dict or string, set an explicit `input_template` on each node.
- Pin the input shape by setting `input_schema` (JSON Schema) on the `begin` node; `graph_input` is then validated at session-create and a mismatch returns `422`.

## Edge kinds

**Static edge** (unconditional):

```json
{"kind": "static", "from_node": "start", "to_node": "next"}
```

**Conditional edge** (router-driven):

```json
{
  "kind": "conditional",
  "from_node": "judge",
  "router": {
    "kind": "json_path",
    "branches": [
      {"conditions": [{"path": "status", "op": "eq", "value": "accept"}], "to_node": "done"},
      {"conditions": [{"path": "status", "op": "eq", "value": "reject"}], "to_node": "producer"}
    ],
    "default_to": "done"
  }
}
```

Router kinds: `json_path` (branch on structured output from `NodeOutput.parsed`) and `callable` (registered Python callable returning a node id).

## Topology rules

The save-time validator enforces these invariants and returns `422` with a description of the violated rule on failure:

- All node ids must be unique within the graph.
- Exactly one `begin` node; at least one `end` node.
- `begin` has no incoming edges. `end` nodes have no outgoing edges.
- Every `end` node must be reachable via BFS from `begin`.
- Every edge endpoint (`from_node`, `to_node`, router branch `to_node`, `default_to`) must reference an existing node id.
- `fan_out` nodes must not have outgoing `edges` entries; targets live on `specs`.
- Cyclic graphs currently accepted without `max_iterations` (no static cycle detection); a future validator may require it.

## Graph-bound sessions

To run a graph, create a workspace session with a graph binding:

```json
{
  "binding": {"kind": "graph", "graph_id": "research-pipeline"},
  "auto_start": false
}
```

Graph-bound sessions run on any workspace backend (local, container, or
kubernetes). The graph executor persists per-node state through the
workspace's git-backed state repository on whichever backend hosts the
workspace.

See the Sessions API for details on session lifecycle.

## Common patterns

The node kinds plus templated inputs compose into a range of
orchestration shapes. The most common:

- **Linear pipeline** - `begin -> agent -> agent -> end`; each node's
  `input_template` reads `nodes.<prev>.text`.
- **Conditional branch** - an `agent` with `response_format` plus a
  `conditional` edge whose `json_path` router branches on the parsed
  output (a classify-then-dispatch state machine).
- **Scatter-gather (map-reduce)** - a `fan_out` `map` spec spawns one
  instance of a target per list item (each reads `{{ fanout_item }}`),
  and a `fan_in` aggregates them.
- **Best-of-N** - a `fan_out` `broadcast` runs N copies of one agent;
  a `fan_in` (or a following agent) selects the best.
- **Multi-lens** - a `fan_out` `tee` runs several different agents on
  the same input; a `fan_in` merges.
- **Iterative refinement** - a cycle (writer -> critic -> writer) with
  `max_iterations`, the writer detecting its first pass with
  `nodes.critic is defined` and the critic's structured flag driving a
  `json_path` router.
- **Tool-augmented** - `tool_call` nodes (with templated `arguments`)
  interleaved with agents for deterministic steps without an LLM turn.

### Scatter-gather example

The `fan_out` node has no outgoing edge (its `map` spec wires
`scatter -> classify`); each `classify` instance follows its own edge
into the `fan_in`, which waits for all instances.

```json
{
  "id": "map-reduce-reviews",
  "description": "Classify each review in parallel, then aggregate.",
  "nodes": [
    {"id": "start", "kind": "begin"},
    {"id": "split", "kind": "agent", "agent_id": "list-emitter",
     "input_template": "Return the reviews as a JSON array:\n{{ initial_input.reviews }}",
     "response_format": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}}}},
    {"id": "scatter", "kind": "fan_out",
     "specs": [{"kind": "map", "target_node_id": "classify", "source_node_id": "split", "source_path": "items"}]},
    {"id": "classify", "kind": "agent", "agent_id": "sentiment",
     "input_template": "Classify review #{{ fanout_index }}:\n{{ fanout_item }}"},
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
```

### Iterative-refinement example

The cycle `write -> critique -> write` requires `max_iterations`. The
writer detects its first pass with `nodes.critique is defined` (not
`iteration == 0` - the first `write` runs at iteration 1, since `begin`
is iteration 0); the critic's structured `done` flag drives a
`json_path` router that ends or loops back.

```json
{
  "id": "draft-and-refine",
  "description": "Draft, critique, revise up to a cap.",
  "max_iterations": 6,
  "nodes": [
    {"id": "start", "kind": "begin"},
    {"id": "write", "kind": "agent", "agent_id": "writer",
     "input_template": "{% if nodes.critique is defined %}Revise using:\n{{ nodes.critique.text }}\n\nDraft:\n{{ nodes.write.text }}{% else %}Draft about: {{ initial_input.topic }}{% endif %}"},
    {"id": "critique", "kind": "agent", "agent_id": "critic",
     "input_template": "Critique; set done=true when good enough:\n{{ nodes.write.text }}",
     "response_format": {"type": "object", "properties": {"done": {"type": "boolean"}}}},
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
```

## Create a graph

`POST /v1/graphs` - returns `201 Created`.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/graphs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "research-pipeline",
    "description": "Research then summarize.",
    "nodes": [
      {"kind": "begin", "id": "start"},
      {"kind": "agent", "id": "researcher", "agent_id": "research-agent"},
      {"kind": "agent", "id": "summarizer", "agent_id": "summary-agent",
       "input_template": "Summarize: {{ nodes.researcher.text }}"},
      {"kind": "end", "id": "done", "output_template": "{{ nodes.summarizer.text }}"}
    ],
    "edges": [
      {"kind": "static", "from_node": "start", "to_node": "researcher"},
      {"kind": "static", "from_node": "researcher", "to_node": "summarizer"},
      {"kind": "static", "from_node": "summarizer", "to_node": "done"}
    ]
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/graphs",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "research-pipeline",
        "description": "Research then summarize.",
        "nodes": [
            {"kind": "begin", "id": "start"},
            {"kind": "agent", "id": "researcher", "agent_id": "research-agent"},
            {"kind": "agent", "id": "summarizer", "agent_id": "summary-agent",
             "input_template": "Summarize: {{ nodes.researcher.text }}"},
            {"kind": "end", "id": "done", "output_template": "{{ nodes.summarizer.text }}"},
        ],
        "edges": [
            {"kind": "static", "from_node": "start", "to_node": "researcher"},
            {"kind": "static", "from_node": "researcher", "to_node": "summarizer"},
            {"kind": "static", "from_node": "summarizer", "to_node": "done"},
        ],
    },
)
assert r.status_code == 201
--- javascript
const r = await fetch("/v1/graphs", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "research-pipeline",
    description: "Research then summarize.",
    nodes: [
      {kind: "begin", id: "start"},
      {kind: "agent", id: "researcher", agent_id: "research-agent"},
      {kind: "agent", id: "summarizer", agent_id: "summary-agent",
       input_template: "Summarize: {{ nodes.researcher.text }}"},
      {kind: "end", id: "done", output_template: "{{ nodes.summarizer.text }}"}
    ],
    edges: [
      {kind: "static", from_node: "start", to_node: "researcher"},
      {kind: "static", from_node: "researcher", to_node: "summarizer"},
      {kind: "static", from_node: "summarizer", to_node: "done"}
    ]
  })
})
```

Response `201 Created` - the full graph object.

**Errors:**
- `409` - a graph with this `id` already exists
- `422` - topology validation failed (see Topology rules above)

## Get a graph

`GET /v1/graphs/{id}` - returns `200 OK` with the graph object.

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/graphs/research-pipeline \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/graphs/research-pipeline",
              headers={"Authorization": f"Bearer {token}"})
--- javascript
const r = await fetch("/v1/graphs/research-pipeline", {
  headers: {"Authorization": `Bearer ${token}`}
})
```

**Errors:** `404` if the id does not exist.

## List graphs

`GET /v1/graphs` - returns an offset or cursor page of graph objects.

Query parameters: `limit` (1-200, default 20), `offset` (default 0), `cursor`, `order_by`.

```code-tabs:curl,python,javascript
--- curl
curl "https://your-host/v1/graphs?limit=50&offset=0" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/graphs",
              headers={"Authorization": f"Bearer {token}"},
              params={"limit": 50, "offset": 0})
page = r.json()
graphs = page["items"]
--- javascript
const r = await fetch("/v1/graphs?limit=50&offset=0", {
  headers: {"Authorization": `Bearer ${token}`}
})
const {items, total, length, offset} = await r.json()
```

## Replace a graph

`PUT /v1/graphs/{id}` - full replacement; returns `200 OK` with the updated graph.

The body uses the same schema as `POST`. All fields are replaced; omitted optional fields reset to their defaults.

```code-tabs:curl,python,javascript
--- curl
curl -X PUT https://your-host/v1/graphs/research-pipeline \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "research-pipeline",
    "description": "Updated pipeline.",
    "nodes": [
      {"kind": "begin", "id": "start"},
      {"kind": "agent", "id": "worker", "agent_id": "research-agent"},
      {"kind": "end", "id": "done", "output_template": "{{ nodes.worker.text }}"}
    ],
    "edges": [
      {"kind": "static", "from_node": "start", "to_node": "worker"},
      {"kind": "static", "from_node": "worker", "to_node": "done"}
    ]
  }'
--- python
import httpx
r = httpx.put(
    "https://your-host/v1/graphs/research-pipeline",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "research-pipeline",
        "description": "Updated pipeline.",
        "nodes": [
            {"kind": "begin", "id": "start"},
            {"kind": "agent", "id": "worker", "agent_id": "research-agent"},
            {"kind": "end", "id": "done", "output_template": "{{ nodes.worker.text }}"},
        ],
        "edges": [
            {"kind": "static", "from_node": "start", "to_node": "worker"},
            {"kind": "static", "from_node": "worker", "to_node": "done"},
        ],
    },
)
--- javascript
await fetch("/v1/graphs/research-pipeline", {
  method: "PUT",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "research-pipeline",
    description: "Updated pipeline.",
    nodes: [
      {kind: "begin", id: "start"},
      {kind: "agent", id: "worker", agent_id: "research-agent"},
      {kind: "end", id: "done", output_template: "{{ nodes.worker.text }}"}
    ],
    edges: [
      {kind: "static", from_node: "start", to_node: "worker"},
      {kind: "static", from_node: "worker", to_node: "done"}
    ]
  })
})
```

**Errors:** `404` if not found, `409` if managed by a harness, `422` on topology violation.

## Delete a graph

`DELETE /v1/graphs/{id}` - returns `204 No Content`.

```code-tabs:curl,python,javascript
--- curl
curl -X DELETE https://your-host/v1/graphs/research-pipeline \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.delete("https://your-host/v1/graphs/research-pipeline",
                 headers={"Authorization": f"Bearer {token}"})
assert r.status_code == 204
--- javascript
await fetch("/v1/graphs/research-pipeline", {
  method: "DELETE",
  headers: {"Authorization": `Bearer ${token}`}
})
```

## Validate graph status

`GET /v1/graphs/{id}/status` - returns `200 OK` with `{"ok": bool, "issues": [string, ...]}`. Checks that every `agent` node's `agent_id` resolves to an existing Agent row and every `graph` node's `graph_id` resolves to an existing Graph row. The walk is depth-1 only (does not recurse into sub-graph nodes). Status is re-evaluated on every call.

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/graphs/research-pipeline/status \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/graphs/research-pipeline/status",
              headers={"Authorization": f"Bearer {token}"})
body = r.json()
# body: {"ok": true, "issues": []}
--- javascript
const r = await fetch("/v1/graphs/research-pipeline/status", {
  headers: {"Authorization": `Bearer ${token}`}
})
const {ok, issues} = await r.json()
```

**Errors:** `404` if the graph id does not exist.

## Errors note

All error responses use the RFC 7807 `ProblemDetails` envelope with `type`, `title`, `status`, `detail`, `instance`, and `extensions` (which includes `request_id` and, for 422 errors, an `errors` array with field paths). See the REST API overview for details.
