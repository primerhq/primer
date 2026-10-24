---
title: "Create and edit a graph"
summary: "Write a graph with its templates and routers, validate it, and manage graphs with the graph tools."
order: 2
tools: [crud__create_graph, crud__update_graph, system__list_graphs, system__get_graph, system__create_graph, system__update_graph, system__delete_graph, system__find_graphs]
related: [graphs/what-a-graph-is, graphs/run-a-graph, approvals/policies]
---
## What it is
You write a graph as one JSON body (nodes, edges and caps) and store it with a graph tool. This page covers the templates that feed each node, the routers that pick the next one, and the tools that create, read, change and delete graphs. The node and edge kinds are on [what a graph is](graphs/what-a-graph-is).

## Concepts
- Templates are sandboxed Jinja with `StrictUndefined`: a missing name or key raises an error instead of rendering empty. Every template sees four names:
  - `initial_input`: the run's input, as it was passed: an object, a string or a list of messages. It is never `input`.
  - `iteration`: the step count. `begin` is 0, so the first node after it runs at 1.
  - `nodes`: the output of every node that already ran, by id: `nodes.extract.text`, `nodes.triage.parsed.category`.
  - `ctx`: facts about the run: `ctx.workspace_id`, `ctx.session_id`, `ctx.graph_id`, `ctx.artifact_dir` (`artifacts/<session id>`, a folder for this run's files) and `ctx.now`.
- A copy started by a `fan_out` also sees `fanout_index` (null for `tee`) and `fanout_item` (the list item for `map`, the fan-out's output for `broadcast` and `tee`).
- Guard a node that may not have run with `{% if nodes.bug is defined %}`, and an optional key with `{{ initial_input.note | default('') }}`.
- The default `input_template` of an `agent` or `graph` node reads a list of messages. When the input is an object or a string, write the template yourself (`{{ initial_input.text }}`, `{{ initial_input }}`).
- Two filters help: `fromjson` parses JSON text (`{{ (nodes.search.text | fromjson)[0].url }}`), and `strip_fences` takes code out of markdown fences.
- In a `tool_call` node, each string leaf of `arguments` is rendered and stays a string; numbers, booleans and nulls pass through unchanged. For a computed number or list, use `arguments_template`, which must render a JSON object.
- One reusable graph can take its instructions from the input: send `{"document": "...", "instructions": {"extract": "...", "summarise": "..."}}` and let each node read `{{ initial_input.instructions.extract }}`. Pin the shape with the `begin` node's `input_schema`.
- Two doors make the same write with the same checks: the `crud` pair (`crud__create_graph`, `crud__update_graph`) and the `system` set, which adds list, get, find and delete. All graph writes, through either door, are approval-gated by default: in a session the call parks until a person approves it ([approval policies](approvals/policies)). The reads ship ungated.
- An update replaces the whole graph. Read it, change it, and send the full body back.
- A new graph's `id` is optional: omitted, the server assigns `graph-<hex>`. A given id must match `[a-z0-9._-]+` and not be `.` or `..`. It never changes.

## How to
1. Search first: a graph that does the job may exist (`system__find_graphs`, `system__list_graphs`).
2. Name the agents each `agent` node runs, and give a `response_format` to every node a router or a `map` reads.
3. Write the templates. Guard every node that a branch may skip.
4. Add `max_iterations` to any graph with a loop.
5. Create the graph. A body that breaks a reference rule is refused with a `validation-error` naming it.
6. Check that it can run: a session create refuses a graph that cannot run, with every problem in one message ([run a graph](graphs/run-a-graph)).
7. To change it, get it, edit the body and update it whole.

## Tool calls
### Create an extract-then-summarise graph
`crud__create_graph` stores an extract-then-summarise graph. The first agent answers JSON, the second reads it, and a `tool_call` node saves the summary to the run's folder; `force` passes through as a boolean.

```json
{"toolset_id": "crud", "tool_name": "create_graph", "arguments": {"entity": {"id": "extract-then-summarise", "description": "Extract the entities of a document, then summarise around them.",
"nodes": [
{"kind": "begin", "id": "start", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
{"kind": "agent", "id": "extract", "agent_id": "entity-extractor", "input_template": "List the entities in this text:\n{{ initial_input.text }}", "response_format": {"type": "object", "properties": {"entities": {"type": "array", "items": {"type": "string"}}}, "required": ["entities"]}},
{"kind": "agent", "id": "summarise", "agent_id": "summariser", "input_template": "Summarise the text around these entities: {{ nodes.extract.parsed.entities | join(', ') }}\n\n{{ initial_input.text }}"},
{"kind": "tool_call", "id": "save", "tool_id": "workspace__write", "arguments": {"path": "{{ ctx.artifact_dir }}/summary.md", "content": "{{ nodes.summarise.text }}", "force": true}},
{"kind": "end", "id": "done", "output_template": "{{ nodes.summarise.text }}"}],
"edges": [
{"kind": "static", "from_node": "start", "to_node": "extract"},
{"kind": "static", "from_node": "extract", "to_node": "summarise"},
{"kind": "static", "from_node": "summarise", "to_node": "save"},
{"kind": "static", "from_node": "save", "to_node": "done"}]}}}
```

Reference: [`crud__create_graph`](reference/tools/crud/create_graph)

### Create a graph that routes
`system__create_graph` stores a triage graph. The `json_path` router reads `parsed.category` of the triage node, and the `end` node guards the branch that did not run.

```json
{"toolset_id": "system", "tool_name": "create_graph", "arguments": {"entity": {"id": "triage-and-route", "description": "Triage an inbound message, then hand it to the bug or the question handler.",
"nodes": [
{"kind": "begin", "id": "start"},
{"kind": "agent", "id": "triage", "agent_id": "triager", "input_template": "Classify this message:\n{{ initial_input.text }}", "response_format": {"type": "object", "properties": {"category": {"enum": ["bug", "question"]}}, "required": ["category"]}},
{"kind": "agent", "id": "bug", "agent_id": "bug-handler", "input_template": "{{ initial_input.text }}"},
{"kind": "agent", "id": "question", "agent_id": "support-handler", "input_template": "{{ initial_input.text }}"},
{"kind": "end", "id": "done", "output_template": "{% if nodes.bug is defined %}{{ nodes.bug.text }}{% else %}{{ nodes.question.text }}{% endif %}"}],
"edges": [
{"kind": "static", "from_node": "start", "to_node": "triage"},
{"kind": "conditional", "from_node": "triage", "router": {"kind": "json_path", "branches": [{"conditions": [{"path": "category", "op": "eq", "value": "bug"}], "to_node": "bug"}], "default_to": "question"}},
{"kind": "static", "from_node": "bug", "to_node": "done"},
{"kind": "static", "from_node": "question", "to_node": "done"}]}}}
```

Reference: [`system__create_graph`](reference/tools/system/create_graph)

### Update a graph into a loop
`crud__update_graph` replaces `draft-and-refine` with a write and critique loop. The writer tests `nodes.critique is defined` to tell its first pass, not `iteration`. At the cap, `on_max_iterations` lands on `done` instead of failing.

```json
{"toolset_id": "crud", "tool_name": "update_graph", "arguments": {"id": "draft-and-refine", "entity": {"id": "draft-and-refine", "description": "Draft, critique and revise up to a cap.", "max_iterations": 6, "on_max_iterations": "done",
"nodes": [
{"kind": "begin", "id": "start"},
{"kind": "agent", "id": "write", "agent_id": "writer", "input_template": "{% if nodes.critique is defined %}Revise your draft using this critique:\n{{ nodes.critique.text }}\n\nDraft:\n{{ nodes.write.text }}{% else %}Write a first draft about: {{ initial_input.topic }}{% endif %}"},
{"kind": "agent", "id": "critique", "agent_id": "critic", "input_template": "Critique this draft. Set done to true only if it needs no more work.\n{{ nodes.write.text }}", "response_format": {"type": "object", "properties": {"done": {"type": "boolean"}, "notes": {"type": "string"}}, "required": ["done", "notes"]}},
{"kind": "end", "id": "done", "output_template": "{{ nodes.write.text }}"}],
"edges": [
{"kind": "static", "from_node": "start", "to_node": "write"},
{"kind": "static", "from_node": "write", "to_node": "critique"},
{"kind": "conditional", "from_node": "critique", "router": {"kind": "json_path", "branches": [{"conditions": [{"path": "done", "op": "eq", "value": true}], "to_node": "done"}], "default_to": "write"}}]}}}
```

Reference: [`crud__update_graph`](reference/tools/crud/update_graph)

### Update through the system toolset
`system__update_graph` makes the same replace. Here it moves one agent node of a small graph to another model profile with `profile_id`.

```json
{"toolset_id": "system", "tool_name": "update_graph", "arguments": {"id": "summarise-ticket", "entity": {"id": "summarise-ticket", "description": "Summarise one support ticket.",
"nodes": [
{"kind": "begin", "id": "start"},
{"kind": "agent", "id": "summarise", "agent_id": "summariser", "profile_id": "fast-local", "input_template": "Summarise this ticket in three lines:\n{{ nodes.start.text }}"},
{"kind": "end", "id": "done", "output_template": "{{ nodes.summarise.text }}"}],
"edges": [
{"kind": "static", "from_node": "start", "to_node": "summarise"},
{"kind": "static", "from_node": "summarise", "to_node": "done"}]}}}
```

Reference: [`system__update_graph`](reference/tools/system/update_graph)

### Get a graph
`system__get_graph` returns the full body: the starting point of every update.

```json
{"toolset_id": "system", "tool_name": "get_graph", "arguments": {"id": "draft-and-refine"}}
```

Reference: [`system__get_graph`](reference/tools/system/get_graph)

### Find graphs
`system__find_graphs` filters by field. This finds graphs whose description mentions summaries, in any case.

```json
{"toolset_id": "system", "tool_name": "find_graphs", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "description"}, "op": "~=*", "right": {"kind": "value", "value": "%summar%"}}}}
```

Reference: [`system__find_graphs`](reference/tools/system/find_graphs)

### List graphs
`system__list_graphs` pages through every graph.

```json
{"toolset_id": "system", "tool_name": "list_graphs", "arguments": {"limit": 20}}
```

Reference: [`system__list_graphs`](reference/tools/system/list_graphs)

### Delete a graph
`system__delete_graph` removes a graph that nothing uses any more.

```json
{"toolset_id": "system", "tool_name": "delete_graph", "arguments": {"id": "summarise-ticket"}}
```

Reference: [`system__delete_graph`](reference/tools/system/delete_graph)

## Gotchas
- A template error fails the run, it does not render empty: `{{ input.text }}`, a key the input lacks, or a node on a branch that did not run.
- `iteration` is the global step count. Tell a loop's first pass with `nodes.<other> is defined`, not `iteration == 0`.
- A router reads `parsed`, so its source node needs a `response_format`. Without it every condition is false and the router always takes `default_to`.
- A rendered argument is a string: `"count": "{{ nodes.plan.parsed.n }}"` sends `"5"`, and a tool that wants a number refuses it.
- A delete is refused with a `conflict` (`in_use_by`) while another graph's `graph` node names the graph, a session that has not ended is bound to it, or a trigger subscription targets it. End or remove those first. A graph that names itself does not block its own delete.
- A save does not check the agents, sub-graphs and tools the nodes name. A missing one fails the run at its node.
- Over MCP nothing can park, so a gated write is refused unless the operator retunes its gate. `system__describe_tool` reports the live gate.

## Related
[what a graph is](graphs/what-a-graph-is), [run a graph](graphs/run-a-graph), [approval policies](approvals/policies).
