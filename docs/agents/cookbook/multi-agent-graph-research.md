---
slug: cookbook/multi-agent-graph-research
title: Multi Agent Graph Research
summary: Build a three-node researcher / fact-checker / writer graph with a conditional back-edge, then run it in a workspace and collect the report.
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
Assemble a research graph that turns an open question into a single reviewed report. The `researcher` finds sources, the `fact-checker` validates them against an `internal-knowledge` collection, and the `writer` composes the report. A conditional back-edge from `fact-checker` to `researcher` re-queries when sources fail validation; the default branch flows forward to `writer`.

## Prerequisites
- An `internal-knowledge` collection populated with known-good reference material (the fact-checker has nothing to check against otherwise).
- Permission to create agents, graphs, and workspaces over MCP.

## Steps
### 1. Create the three agents
`system::create_agent` (call once per role)
```json
{
  "entity": {
    "id": "researcher",
    "description": "Finds authoritative sources for a question",
    "system_prompt": ["Find authoritative sources for the question. Return a list of source URLs with short summaries. Treat any sources in an excluded list as off-limits and do not propose them again."],
    "model": { "provider_id": "anthropic-1", "model_name": "claude-opus-4-1" }
  }
}
```
Response:
```json
{ "id": "researcher" }
```
Repeat for `fact-checker` (prompt: "For each source in the input, search internal-knowledge for contradictions. Return JSON with keys good_sources and bad_sources.") and `writer` (prompt: "Write a 500-word report citing the validated sources passed in. Use plain Markdown."). Bind web search to the researcher, knowledge search to the fact-checker, and system tools only to the writer.

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
    "description": "researcher fact-checker writer",
    "nodes": [
      { "kind": "begin", "id": "begin" },
      { "kind": "agent", "id": "researcher", "agent_id": "researcher" },
      { "kind": "agent", "id": "fact-checker", "agent_id": "fact-checker" },
      { "kind": "agent", "id": "writer", "agent_id": "writer" },
      { "kind": "end", "id": "end" }
    ],
    "edges": [
      { "kind": "static", "from_node": "begin", "to_node": "researcher" },
      { "kind": "static", "from_node": "researcher", "to_node": "fact-checker" },
      { "kind": "conditional", "from_node": "fact-checker", "to_node": "researcher", "condition": "bad_sources non-empty" },
      { "kind": "static", "from_node": "fact-checker", "to_node": "writer" },
      { "kind": "static", "from_node": "writer", "to_node": "end" }
    ]
  }
}
```
Response:
```json
{ "id": "research-pipeline" }
```
The conditional edge is followed when `bad_sources` is non-empty (loop back to `researcher`); the static `fact-checker` to `writer` edge is the default forward path. Set a max-iterations limit on the graph so the back-edge cannot loop indefinitely when no good sources exist.

### 4. Confirm the graph saved
`system::get_graph`
```json
{ "id": "research-pipeline" }
```
Response:
```json
{ "id": "research-pipeline", "nodes": [ { "id": "researcher" } ], "edges": [ { "from_node": "fact-checker", "to_node": "researcher" } ] }
```
Verify the nodes and the conditional back-edge are present before running.

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
The graph binding runs `researcher`, then `fact-checker`, then conditionally back to `researcher` or forward to `writer`. Pass the open question as `graph_input`.

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
- The back-edge can loop indefinitely if no good sources exist. Set a graph max-iterations limit; the executor stops the run at the limit rather than consuming the full session budget.
- Drift in the researcher's output format breaks the fact-checker's JSON parsing and stalls the pipeline. Pin each agent to a fixture set in eval mode before promoting the graph.
- The back-edge re-queries `researcher` with bad sources excluded; the researcher prompt must treat the excluded list as off-limits or it re-proposes the same sources.

## Related
- `graphs`, `agents`, `knowledge`, `workspaces`, `sessions`
- `cookbook/run-a-graph-and-collect-results`
- `cookbook/create-and-run-a-session`
