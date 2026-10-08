---
slug: cookbook/multi-agent-graph-research
title: Multi Agent Graph Research
summary: Build a researcher / fact-checker / verdict / writer graph with a conditional back-edge that re-queries on failed sources, then run it in a workspace and collect the report.
mcp_tools:
  - system::create_agent
  - system::create_collection
  - system::create_graph
  - system::get_graph
  - workspaces::create_workspace
  - workspaces::create_workspace_session
  - workspaces::get_workspace_session
---

## Goal
Assemble a research graph that turns an open question into a single reviewed report. The `researcher` finds sources, the `fact-checker` checks them against an `internal-knowledge` collection, a small `verdict` step turns the findings into JSON, and the `writer` composes the report. A conditional back-edge from `verdict` to `researcher` re-queries when sources fail validation; the default branch flows forward to `writer`.

Why there is a `verdict` step: a `json_path` router reads only a node's structured (`parsed`) output, and a node that has a `response_format` is offered no tools. The node that searches (`fact-checker`) therefore cannot be the node that routes. It writes its findings as plain text, and `verdict` (no tools, structured output) states them as `{good_sources, bad_sources}` for the router.

## Prerequisites
- An `internal-knowledge` collection populated with known-good reference material (the fact-checker has nothing to check against otherwise).
- A ModelProfile for the agents' model, e.g. `anthropic-1--claude-opus-4-1` (see "Model profiles" in `agents`).
- Permission to create agents, graphs, and workspaces over MCP.

## Steps
### 1. Create the four agents
`system::create_agent` (call once per role)
```json
{
  "entity": {
    "id": "researcher",
    "description": "Finds authoritative sources for a question",
    "system_prompt": ["Find authoritative sources for the question. Return a list of source URLs with short summaries. If the input lists sources that failed validation, treat them as off-limits and do not propose them again."],
    "model": { "profile_id": "anthropic-1--claude-opus-4-1" }
  }
}
```
Response:
```json
{ "id": "researcher" }
```
Repeat for `fact-checker` (prompt: "For each source in the input, search internal-knowledge for contradictions. Say which sources hold up and which are contradicted or cannot be verified, with a line of evidence each. Plain text."), `verdict` (prompt: "Read the fact-checker's findings. Reply with JSON only, no prose: {\"good_sources\": [the sources that hold up], \"bad_sources\": [the sources that were contradicted or could not be verified]}. Use empty lists where none apply."), and `writer` (prompt: "Write a 500-word report citing the validated sources passed in. Use plain Markdown."). Bind web search to the researcher, knowledge search to the fact-checker, and system tools only to the writer. The `verdict` agent needs no tools (its graph node is offered none).

### 2. Ensure the knowledge collection exists
`system::create_collection`
```json
{
  "entity": {
    "id": "internal-knowledge",
    "description": "Known-good reference material",
    "embedder": { "provider_id": "hf-1", "model": "all-MiniLM-L6-v2" },
    "search_provider_id": "ssp-1"
  }
}
```
Response:
```json
{ "id": "internal-knowledge" }
```
Populate it with reference material before the first run.

### 3. Create the graph
`system::create_graph`
```json
{
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
}
```
Response:
```json
{ "id": "research-pipeline" }
```
The conditional edge sends the run back to `researcher` while the verdict's `bad_sources` has a first entry (`bad_sources[0]` exists, that is, the list is non-empty) and on to `writer` otherwise (`default_to`). Three things in the body are what make it run, not just save:
- **Data flows through `input_template`, not through the graph.** A node sees only its own earlier turns and the template it is given. The default template walks the graph input as a list of messages (it fails on a dict like the `graph_input` in step 6) and never mentions another node, so without these templates the fact-checker would never see the researcher's sources and the researcher would never see which ones failed. `initial_input` is the `graph_input` you pass in step 6, and `nodes.<id>.text` / `.parsed` are earlier nodes' outputs. A node id with a hyphen needs the bracket form, `nodes['fact-checker']`, because `nodes.fact-checker` is read as a subtraction.
- **`response_format` on `verdict`** is what fills `parsed`, which is all a `json_path` router reads. A router on a node without one never matches and always takes `default_to`.
- **`max_iterations` is required for a loop.** The graph saves as a draft without it, but `workspaces::create_workspace_session` refuses to start a session on it (422, "cyclic graph ... requires max_iterations"). The cap counts supersteps: the `begin` node is one and each researcher / fact-checker / verdict pass is three, so `10` allows three judged passes (the first and two re-queries). `on_max_iterations: "writer"` makes the cap a landing, not a failure: if the third verdict still lists bad sources, the run goes to `writer` with that verdict rather than ending `failed` with `max_iterations_exceeded`.

### 4. Confirm the graph saved
`system::get_graph`
```json
{ "id": "research-pipeline" }
```
Response:
```json
{ "id": "research-pipeline", "nodes": [ { "id": "researcher" } ], "edges": [ { "kind": "conditional", "from_node": "verdict" } ] }
```
Verify the nodes, the conditional back-edge and `max_iterations` are present before running. Saving a graph does not prove it can run: a graph that is empty, has no `end` node, or loops without a cap is stored as a draft and refused only when a session binds to it (step 6).

### 5. Materialise a workspace
`workspaces::create_workspace`
```json
{ "template_id": "py-base" }
```
Response:
```json
{ "id": "ws-1", "phase": "running" }
```
Wait until `phase` is `running`. A long graph run holds the workspace slot for its duration; use a longer template TTL if the writer step is slow.

### 6. Run the graph
`workspaces::create_workspace_session`
```json
{
  "workspace_id": "ws-1",
  "binding": { "kind": "graph", "graph_id": "research-pipeline" },
  "graph_input": { "question": "How did the SLO methodology evolve from 2018 to 2025?" },
  "auto_start": true
}
```
Response:
```json
{ "id": "ses-1", "status": "running" }
```
The graph binding runs `researcher`, then `fact-checker`, then `verdict`, then conditionally back to `researcher` or forward to `writer`. Pass the open question as `graph_input.question`: the templates in step 3 read `initial_input.question`, and a `graph_input` without that key fails the node (the templates are strict about missing variables).

### 7. Poll until the run ends
`workspaces::get_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "ended", "ended_reason": "completed" }
```
The final `writer` node emits the Markdown report as its last assistant turn.

## Verify
`status` is `ended` with `ended_reason: "completed"`, and the `writer` node's transcript ends with a 500-word Markdown report citing only validated sources. The workspace log shows a git commit per node, so each stage's output can be diffed against the previous one.

## Gotchas
- The back-edge can loop until the cap if no good sources exist. With `max_iterations: 10` and `on_max_iterations: "writer"` the run ends `completed` after three judged passes, and the report is written from whatever the last verdict held (possibly nothing validated). Drop `on_max_iterations` if you would rather the run end `failed` (`max_iterations_exceeded`) than write an under-sourced report. Raise the cap in steps of three (13, 16, ...) to allow more passes.
- A `verdict` reply that is not JSON does not loop: `parsed` is empty, no branch matches, and the run takes `default_to` to the writer with the raw reply. A model that wraps its JSON in a single code fence is tolerated (the executor strips it); prose around the JSON is not. Pin the `verdict` agent to a fixture set in eval mode before promoting the graph.
- The re-query only works if the researcher's `input_template` lists the failed sources, as in step 3, and its prompt treats them as off-limits; otherwise it re-proposes the same sources.
- The `researcher` template guards the failed-sources block with `{% if nodes.verdict is defined %}`: on the first pass `verdict` has not run, and referencing a node that has not run raises.

## Related
- `graphs`, `agents`, `knowledge`, `workspaces`, `sessions`
- `cookbook/run-a-graph-and-collect-results`
- `cookbook/create-and-run-a-session`
