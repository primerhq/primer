---
title: "What a graph is"
summary: "A graph is nodes and edges that run agents, tools and sub-graphs in steps: node kinds, edge kinds and node output."
order: 1
related: [graphs/create-and-edit, graphs/run-a-graph, agents/what-an-agent-is]
---
## What it is
A graph is a stored workflow: nodes that run agents, tools or other graphs, joined by edges that decide which node runs next. Use one when a job needs several steps with routing, loops or parallel branches. When one agent with its tools can do the whole job, use the agent instead ([what an agent is](agents/what-an-agent-is)).

You write a graph with the graph tools ([create and edit a graph](graphs/create-and-edit)) and run it in a session ([run a graph](graphs/run-a-graph)).

## Concepts
- A `Graph` row holds `id`, `description`, `nodes`, `edges`, `max_iterations`, `on_max_iterations` and `harness_id`. There is no entry field and no sink field: a run starts at the one `begin` node and stops at an `end` node.
- A run moves in steps (supersteps). Each step runs every ready node at once. When a node finishes, its output joins the run's context, and the edges out of it pick the nodes that run in the next step. `iteration` counts the steps: `begin` runs at 0, so the first node after it runs at 1.
- Each node builds its input from a Jinja template over that context, so a node can read the run's input and the output of any node that already ran. The templates are on [create and edit a graph](graphs/create-and-edit).
- A graph runs in one workspace. Every node shares it, so nodes can pass files as well as template data. Agent nodes follow the workspace's `agents.md` unless the agent sets `use_workspace_instructions` to false.

### Node kinds
Every node has an `id`, unique in the graph, and a `kind`. Its other fields sit beside them: there is no `config` wrapper. An optional `description` labels it.
- `begin`: the one entry node. It calls no model. An optional `input_schema` (JSON Schema) checks the run's input when the session is created. No edge may point into it.
- `agent`: runs a stored agent for one turn. `agent_id` names it. `input_template` renders the user message the agent gets. `response_format` (a JSON Schema) makes the turn answer JSON, which fills `parsed`; such a node is a data-shaping turn and is offered no tools. `profile_id` runs this node on another model profile than the agent's default. `input_schema` is a hint for designers: a rendered input that breaks it only logs a warning.
- `tool_call`: calls one tool, with no model. `tool_id` is the scoped id (`web__web_search`, `workspace__write`). `arguments` is an object whose string leaves are templates. `arguments_template` is one template that renders the whole JSON object, and it wins over `arguments`. An optional `output_schema` makes the output parse and validate as JSON.
- `graph`: runs another stored graph as one step. `graph_id` names it, and `input_template` renders the one user message it starts from. The child is read when the node runs, so an edit to it reaches the next run.
- `fan_out`: starts parallel copies of other nodes. It has no edges of its own: its `specs` (one or more) name the targets. `broadcast` runs `count` copies of `target_node_id`. `tee` runs each of `target_node_ids` once. `map` reads a list at `source_path` in the `parsed` output of `source_node_id` and runs one copy of `target_node_id` per item. `on_failure` says what one failed copy does: `fail_fast` (the default) ends the run, `drain_then_fail` lets the siblings finish and then fails it, `collect` marks the copy and goes on.
- `fan_in`: waits for every incoming branch (every copy of a fan-out target), then renders `aggregate_template` into its `text`. An optional `output_schema` checks the result.
- `end`: renders `output_template` into the graph's output. An optional `output_schema` requires that output to be JSON that fits it. No edge may leave it. A graph may have several.

### Edge kinds
- `static`: `from_node` and `to_node`. It always fires when `from_node` finishes.
- `conditional`: `from_node` and a `router` that picks the next node.
  - A `json_path` router holds ordered `branches`. Each branch has `conditions` (all must hold) and a `to_node`; the first branch that matches wins, and an empty `conditions` list matches anything. A condition is a `path` into the source node's `parsed` (`a.b[2].c`), an `op` (`eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `exists`) and a `value`. When no branch matches, `default_to` fires; with no `default_to` the run fails with `routing_failed`.
  - A `callable` router names a `callable_id` that server code registers. Tools cannot register one, and a stock install registers none, so use `json_path`.

### Node output
When a node finishes, `nodes.<id>` holds its output:
- `text`: the agent's last answer, the tool's output, or the rendered template of a `fan_in` or `end`. For `begin` it is the input as text, whatever its shape.
- `parsed`: a JSON object, or null. An agent node fills it only with a `response_format` (an answer that is JSON but not an object comes back as `{"value": ...}`). A `tool_call` node fills it only with an `output_schema` and an object answer. `begin` fills it when the input is an object.
- `history`: the node's message history.
- `iteration`: the step that produced it.
- `error` and `ended_detail`: set only on a fan-out copy that failed under `collect`.

A fan-out target is a list: `nodes.<target>` holds every copy in index order (even for `tee`), and each copy is also at `nodes['<target>[i]']`.

### What makes a graph runnable
A create or update refuses a body that breaks a reference rule: node ids repeat; an edge, branch, `default_to` or `on_max_iterations` names a node that does not exist; an edge points into `begin` or leaves an `end`; a `fan_out` has edges of its own, or targets `begin` or another `fan_out`; a `map` source is itself a fan-out target; a `fan_in` has no incoming edge; a schema is not valid JSON Schema.

The rest is checked only when a session binds the graph: at least one node, exactly one `begin`, at least one `end`, every `end` reachable from `begin`, and `max_iterations` set when the graph has a cycle or a `callable` router. A graph that breaks these still saves, as a draft. The session create refuses it with a `validation-error` that lists every problem at once.

### How a run ends
- It completes when no node is ready. Every `end` fires when it is reached; the first one does not stop the others.
- It fails with a code, such as `tool_execution_failed`, `tool_output_invalid`, `end_output_invalid`, `template_error`, `routing_failed`, `fanout_source_invalid`, `fanin_upstream_failed`, `tool_turn_cap`, `max_iterations_exceeded`. An approval on a `tool_call` node that is refused, times out or is cancelled fails it with `tool_approval_rejected`, `tool_approval_timeout` or `tool_approval_cancelled`.
- `max_iterations` caps the steps. When the cap is hit, the run fails with `max_iterations_exceeded`, unless `on_max_iterations` names a node: then the run jumps once to that node (a finalize or report node) and goes on to an `end`.
- A node that needs a person (a `tool_call` on `system__ask_user`, an agent that asks, or an approval gate) parks the whole run until the answer. See [run a graph](graphs/run-a-graph).

## Gotchas
- `json_path` routers and `map` specs read `parsed`, never `text`. A source node with no `response_format` (or no `output_schema`, for a tool) has a null `parsed`: every condition is false and the router always takes `default_to`.
- A path that does not resolve makes every operator false, `ne` and `not_in` included. Test presence with `exists`.
- Agent nodes do not auto-compact. A node keeps its own history across the passes of a loop and sends it whole every pass. Bound loops with `max_iterations` and keep tool output short.
- An agent node that stops at its agent's `max_tool_turns` fails the run with `tool_turn_cap`.
- A save does not check that an `agent_id`, `graph_id` or `tool_id` exists. A missing one fails the run when its node runs.
- A graph whose `harness_id` is set belongs to a harness: the graph tools refuse to change it.

## Related
[create and edit a graph](graphs/create-and-edit), [run a graph](graphs/run-a-graph), [what an agent is](agents/what-an-agent-is).
