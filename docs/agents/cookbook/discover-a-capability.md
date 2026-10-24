---
slug: cookbook/discover-a-capability
title: Discover A Capability
summary: Find the right tool, agent, graph, or doc for a task you do not yet know how to do.
mcp_tools:
  - system::wiki_search
  - system::wiki_read
  - system::describe_tool
---

## Goal
Go from a vague task ("I want to do X") to a concrete `toolset::tool` id plus the doc that documents it.

## Prerequisites
- None. These are read-only calls on the `system` collection, which every install has. A seeded agent holds `system::wiki_search` and `system::wiki_read` directly; over MCP the operator must allowlist them (`system__wiki_search`, `system__wiki_read`).

## Steps
### 1. Search the docs and guides
`system::wiki_search`
```json
{ "query": "how do I run an agent headlessly", "limit": 3 }
```
Response:
```json
{ "hits": [ { "path": "docs/sessions", "excerpt": "...", "score": 0.81 } ], "truncated": false, "mode_used": "semantic" }
```
Each hit is a page of the `system` collection: a guide, a tool's reference page or one of these docs (`docs/<slug>`). `mode_used` is `fulltext` (keyword search) until an admin turns internal search on.

### 2. Read the matched doc
`system::wiki_read`
```json
{ "path": "docs/sessions" }
```
Response:
```json
{ "path": "docs/sessions", "title": "...", "summary": "...", "kind": "...", "body": "...", "children": [] }
```
The whole doc comes back, not a chunk of it.

### 3. Find the exact tool
`system::wiki_search`
```json
{ "query": "start a session inside a workspace", "path_prefix": "reference/tools/", "limit": 3 }
```
Response:
```json
{ "hits": [ { "path": "reference/tools/workspaces/create_workspace_session", "excerpt": "...", "score": 0.79 } ], "truncated": false, "mode_used": "semantic" }
```
A tool's reference page is `reference/tools/<toolset>/<tool>`. Read its exact arguments and the call to make:

`system::describe_tool`
```json
{ "toolset_id": "workspaces", "tool_name": "create_workspace_session" }
```

### 4. Find an agent or graph to run
`system::wiki_search`
```json
{ "query": "review a pull request", "path_prefix": "this-install/agents/", "limit": 3 }
```
Response:
```json
{ "hits": [ { "path": "this-install/agents/code-reviewer", "excerpt": "...", "score": 0.74 } ], "truncated": false, "mode_used": "semantic" }
```
Use `"path_prefix": "this-install/graphs/"` the same way to find a multi-step graph instead of a single agent. `system::wiki_search` leaves `this-install/` out unless the prefix names it.

## Verify
You hold a concrete `toolset::tool` id (e.g. `workspaces::create_workspace_session`) and the path of the doc that documents it (e.g. `docs/sessions`).

## Gotchas
- There is no `search` toolset: notes that name per-entity search tools are stale. Every discovery call reads or searches the `system` collection.
- Search is by keyword until an admin turns internal search on (System › Internal collections); `mode_used` says which rung answered. Keep `mode: "auto"` (the default): `mode: "semantic"` is refused while the index is off.
- The `this-install/` pages are definitions, not running instances; to drive one, see `cookbook/create-and-run-a-session`.
- `collections::search` and `collections::read_document` with `"collection": "system"` reach the same pages, for a caller that holds the collections toolset rather than the wiki tools.

## Related
- `semantic-search`, `agents`, `graphs`
- `cookbook/create-and-run-a-session`
- `cookbook/run-a-graph-and-collect-results`
