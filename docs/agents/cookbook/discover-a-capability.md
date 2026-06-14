---
slug: cookbook/discover-a-capability
title: Discover A Capability
summary: Find the right tool, agent, graph, or doc for a task you do not yet know how to do.
mcp_tools:
  - search::search_ai_docs
  - system::get_document_content
  - search::search_tools
  - search::search_agents
  - search::search_graphs
---

## Goal
Go from a vague task ("I want to do X") to a concrete `toolset::tool` id plus the doc that documents it.

## Prerequisites
- None. These are read-only discovery calls.

## Steps
### 1. Search the docs
`search::search_ai_docs`
```json
{ "query": "how do I run an agent headlessly", "top_k": 3 }
```
Response:
```json
{ "items": [ { "document_id": "sessions", "score": 0.81, "text": "..." } ] }
```
Each hit's `document_id` is a doc slug. Pick the most relevant.

### 2. Fetch the full doc
`system::get_document_content`
```json
{ "id": "sessions", "collection_id": "_internal_ai_docs" }
```
Response:
```json
{ "id": "sessions", "content": "---\nslug: sessions\n..." }
```
The chunk from step 1 is a fragment; this returns the whole document, including its `mcp_tools` frontmatter.

### 3. Find the exact tool
`search::search_tools`
```json
{ "query": "start a session inside a workspace", "top_k": 3 }
```
Response:
```json
{ "items": [ { "id": "workspaces::create_workspace_session", "score": 0.79 } ] }
```
This returns ranked tool ids you can call directly.

### 4. Find an agent or graph to run
`search::search_agents`
```json
{ "query": "review a pull request", "top_k": 3 }
```
Response:
```json
{ "items": [ { "id": "code-reviewer", "score": 0.74 } ] }
```
Use `search::search_graphs` the same way to find a multi-step graph instead of a single agent.

## Verify
You hold a concrete `toolset::tool` id (e.g. `workspaces::create_workspace_session`) and the slug of the doc that documents it (e.g. `sessions`).

## Gotchas
- If a search returns `is_error` with `type: "subsystem-inactive"` (no embedder configured), fall back to `system::get_document_content` on a known slug; the capability index in `skills/using-primer-over-mcp.md` lists them.
- `search::search_ai_docs` returns chunks, not whole docs; always follow up with `system::get_document_content` for the full text.
- `search_agents` / `search_graphs` search definitions, not running instances; to drive one, see `cookbook/create-and-run-a-session`.

## Related
- `semantic-search`, `agents`, `graphs`
- `cookbook/create-and-run-a-session`
- `cookbook/run-a-graph-and-collect-results`
