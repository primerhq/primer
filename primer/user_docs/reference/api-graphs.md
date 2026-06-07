---
slug: api-graphs
title: Graphs API
section: reference
summary: REST endpoints to create, list, update, delete, and validate directed agent graphs.
---

A graph is a directed network of typed nodes connected by static or conditional edges. The executor walks nodes in Pregel-style supersteps; each node produces output that downstream nodes consume via Jinja2 templates. Every graph must have exactly one `begin` node and at least one `end` node.

```ref:concepts/what-is-an-agent
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
| `id` | yes | string | User-defined identifier (min length 1, case-sensitive) |
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

See the Sessions API for details on session lifecycle.

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
