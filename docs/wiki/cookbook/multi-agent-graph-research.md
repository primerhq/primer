---
title: "Multi-agent research graph"
summary: "Build a researcher, fact-checker and writer graph with a loop back on failed sources, then run it."
order: 13
tools: [system__create_agent, system__create_collection, system__create_graph, system__get_graph, workspaces__create_workspace, workspaces__create_workspace_session, workspaces__get_workspace_session]
related: [graphs/create-and-edit, graphs/what-a-graph-is]
---
## Goal

Turn an open question into one reviewed report. Four agents run as one graph: `researcher` finds sources, `fact-checker` checks them against a collection, `verdict` states the result as JSON, and `writer` writes the report. When a source fails, the graph loops back to `researcher` with the failure listed; when none fails, it goes on to `writer`.

Why `verdict` is its own step: a `json_path` router reads only a node's `parsed` output, only a node with a `response_format` fills it, and such a node is offered no tools. So the node that searches (`fact-checker`) cannot be the node that routes. It writes plain text, and `verdict` (no tools, structured output) restates it as `{good_sources, bad_sources}` for the router.

## Prerequisites

- Run it from a session in the default workspace (prime). From a session in any other workspace, `workspaces__create_workspace` answers `forbidden`.
- Agent and graph creates are approval-gated by default: in a session, each `system__create_agent` and `system__create_graph` call parks until a person approves it. Over MCP, where nothing can park, a gated call is refused until an admin retunes the gate ([roles and approvals](start-here/roles-and-approvals)).
- A model profile for the agents. The calls use `anthropic-1--claude-sonnet-4-6`; `system__list_model_profile_choices` lists the ones you may pick, and `system__create_agent` refuses a profile that does not exist.
- A web search engine for the researcher. An admin picks it at [web search and fetch settings](providers/web-search-and-fetch).
- A workspace template, here `py-base` (`workspaces__list_workspace_templates`).

## Steps

### 1. Create the four agents

Create each role with `system__create_agent`. The researcher searches and reads the web:

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "researcher", "description": "Finds authoritative sources for a research question.", "system_prompt": ["Find authoritative sources for the question. Return a list of source URLs with short summaries. If the input lists sources that failed validation, treat them as off-limits and do not propose them again."], "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "tools": ["web__web_search", "web__web_fetch"]}}}
```

The fact-checker searches the collection that step 2 creates:

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "fact-checker", "description": "Checks research sources against the internal-knowledge collection.", "system_prompt": ["For each source in the input, search the internal-knowledge collection for contradictions. Say which sources hold up and which are contradicted or cannot be verified, with a line of evidence each. Plain text."], "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "tools": ["collections__search", "collections__read_document"]}}}
```

The verdict agent gets no tools. Its graph node has a `response_format`, and such a node is offered none:

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "verdict", "description": "Turns fact-check findings into a JSON verdict on each source.", "system_prompt": ["Read the fact-checker's findings. Reply with JSON only, no prose: {\"good_sources\": [the sources that hold up], \"bad_sources\": [the sources that were contradicted or could not be verified]}. Use empty lists where none apply."], "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "tools": []}}}
```

The writer needs no tools either:

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "writer", "description": "Writes a short research report from validated sources.", "system_prompt": ["Write a 500-word report that answers the question and cites only the validated sources passed in. Use plain Markdown."], "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "tools": []}}}
```

Each call answers the stored agent. The ids are the `agent_id` values of step 3.

### 2. Create the knowledge collection

Create the collection the fact-checker reads with `system__create_collection`:

```json
{"toolset_id": "system", "tool_name": "create_collection", "arguments": {"entity": {"id": "internal-knowledge", "description": "Known-good reference material for fact-checking research sources."}}}
```

Response:

```json
{"id": "internal-knowledge", "description": "Known-good reference material for fact-checking research sources.", "search": null}
```

With no `search` block the collection is grep-only: `collections__search` in `auto` mode answers from the fulltext rung. Fill it with reference material before the first run ([write documents](knowledge/documents)); the fact-checker has nothing to check against otherwise.

### 3. Create the graph

Store the graph with `system__create_graph`:

```json
{"toolset_id": "system", "tool_name": "create_graph", "arguments": {
  "entity": {
    "id": "research-pipeline",
    "description": "researcher, fact-checker, verdict, writer",
    "max_iterations": 10,
    "on_max_iterations": "writer",
    "nodes": [
      { "kind": "begin", "id": "begin" },
      { "kind": "agent", "id": "researcher", "agent_id": "researcher",
        "input_template": "Question: {{ initial_input.question }}\n{% if nodes.verdict is defined %}Sources that failed validation (do not propose them again):\n{% for s in nodes.verdict.parsed.bad_sources %}- {{ s }}\n{% endfor %}{% endif %}" },
      { "kind": "agent", "id": "fact-checker", "agent_id": "fact-checker",
        "input_template": "Check these sources against internal-knowledge:\n{{ nodes.researcher.text }}" },
      { "kind": "agent", "id": "verdict", "agent_id": "verdict",
        "input_template": "Fact-checker findings:\n{{ nodes['fact-checker'].text }}",
        "response_format": {
          "type": "object",
          "properties": {
            "good_sources": { "type": "array", "items": { "type": "string" } },
            "bad_sources": { "type": "array", "items": { "type": "string" } }
          },
          "required": ["good_sources", "bad_sources"]
        } },
      { "kind": "agent", "id": "writer", "agent_id": "writer",
        "input_template": "Question: {{ initial_input.question }}\nValidated sources (JSON):\n{{ nodes.verdict.text }}" },
      { "kind": "end", "id": "end" }
    ],
    "edges": [
      { "kind": "static", "from_node": "begin", "to_node": "researcher" },
      { "kind": "static", "from_node": "researcher", "to_node": "fact-checker" },
      { "kind": "static", "from_node": "fact-checker", "to_node": "verdict" },
      { "kind": "conditional", "from_node": "verdict", "router": {
          "kind": "json_path",
          "branches": [ { "conditions": [ { "path": "bad_sources[0]", "op": "exists" } ], "to_node": "researcher" } ],
          "default_to": "writer"
      } },
      { "kind": "static", "from_node": "writer", "to_node": "end" }
    ]
  }
}}
```

Response:

```json
{"id": "research-pipeline", "max_iterations": 10, "on_max_iterations": "writer"}
```

The conditional edge sends the run back to `researcher` while the verdict's `bad_sources` has a first entry (the list is not empty), and on to `writer` otherwise (`default_to`). Three things in the body make it run, not just save:

- **Data flows through `input_template`.** A node sees only the template it is given. The default template walks the graph input as a list of messages: it fails on a dict like the `graph_input` of step 6, and it never mentions another node. That is why every node here has its own template. `initial_input` is the `graph_input` of step 6, and `nodes.<id>.text` and `nodes.<id>.parsed` are earlier nodes' outputs. A node id with a hyphen needs the bracket form, `nodes['fact-checker']`: `nodes.fact-checker` reads as a subtraction.
- **`response_format` on `verdict`** fills `parsed`, which is all a `json_path` router reads. A router on a node without one never matches and always takes `default_to`.
- **`max_iterations` is required for a loop.** Without it the graph saves as a draft, and the session create of step 6 refuses it, saying the cycle requires `max_iterations`. The cap counts supersteps: `begin` is one, and each researcher, fact-checker and verdict pass is three, so a cap of 10 allows three judged passes (the first and two re-queries). `on_max_iterations: "writer"` makes the cap a landing, not a failure: if the third verdict still lists bad sources, the run goes to `writer` with that verdict.

### 4. Confirm the graph saved

Read it back with `system__get_graph`:

```json
{"toolset_id": "system", "tool_name": "get_graph", "arguments": {"id": "research-pipeline"}}
```

Response:

```json
{"id": "research-pipeline", "max_iterations": 10, "on_max_iterations": "writer", "nodes": [{"kind": "begin", "id": "begin"}], "edges": [{"kind": "conditional", "from_node": "verdict"}]}
```

Check the nodes, the conditional edge and `max_iterations`. Saving does not prove the graph can run: a graph with a loop and no cap, or with no reachable `end`, is stored as a draft and refused only when a session binds to it.

### 5. Create the workspace

Create a workspace for the run with `workspaces__create_workspace`:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"id": "slo-research", "template_id": "py-base"}}
```

Response:

```json
{"id": "slo-research", "template_id": "py-base", "phase": "pending"}
```

The row starts `pending` and is usable at once. Thread its `id` into the next step.

### 6. Run the graph

Bind a session to the graph and pass the question with `workspaces__create_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "slo-research", "binding": {"kind": "graph", "graph_id": "research-pipeline"}, "graph_input": {"question": "How did the SLO methodology evolve from 2018 to 2025?"}}}
```

Response:

```json
{"id": "sess-5d1e9a", "workspace_id": "slo-research", "status": "running"}
```

Pass the question as `graph_input.question`. The templates read `initial_input.question`, and they are strict about missing names: a `graph_input` without `question` fails the `researcher` node before any agent answers, and the run ends `failed`. To run every agent on one model profile, add `"profile_id"` to the binding; a node's own `profile_id` still wins.

### 7. Poll until the run ends

Read the session with `workspaces__get_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "slo-research", "session_id": "sess-5d1e9a"}}
```

Response:

```json
{"info": {"session_id": "sess-5d1e9a", "status": "ended", "ended_reason": "completed"}, "status": "ended"}
```

A graph session ends by itself when the run ends. Poll while `status` is `running`. A park does not move `status`: a run that waits on a question or an approval still reads `running`, and `workspaces__find_sessions` shows it as `session_state` `parked` with its `parked_event_key`. Only a person answers it.

## Verify

`status` is `ended` with `ended_reason` `completed`. Read the transcript with `workspaces__read_workspace_session_messages`: the records carry the `node_id` of the node that wrote them, and the `writer` node's last `assistant_token` record holds the report.

## Gotchas

- The loop can run to the cap when no good source exists. With a cap of 10 and `on_max_iterations: "writer"`, the run ends `completed` after three judged passes, and the report is written from the last verdict, perhaps with nothing validated. Drop `on_max_iterations` to get a failed run instead of an under-sourced report: the session ends `failed`, and its transcript carries an `error` record with `code` `max_iterations_exceeded` and no `node_id`. Raise the cap in steps of three (13, 16, ...) to allow one more judged pass each.
- A verdict that is not JSON does not loop: `parsed` stays empty, no branch matches, and the run goes to `writer` with the raw reply. A reply wrapped in one code fence still parses; prose around the JSON does not.
- The re-query works only because the researcher's template lists the failed sources and its prompt treats them as off-limits. Without both, it proposes the same sources again.
- The researcher's template guards the failed-sources block with `{% if nodes.verdict is defined %}`: on the first pass `verdict` has not run, and naming a node that has not run raises.
- A message to the session (`workspaces__message_session`) does not reach the graph. It restarts the graph once the run is over, and the run reads only the stored `graph_input` ([run a graph](graphs/run-a-graph)).
- A graph session reads the stored graph when its run starts. An edit with `system__update_graph` reaches the next run, not the one in flight.

## Related

- [Create and edit a graph](graphs/create-and-edit)
- [What a graph is](graphs/what-a-graph-is)
