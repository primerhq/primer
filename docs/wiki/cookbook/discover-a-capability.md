---
title: "Discover a capability"
summary: "Go from a vague task to the exact tool call: search the wiki, read a reference page and describe the tool."
order: 14
tools: [system__wiki_search, system__wiki_read, system__describe_tool, system__find_agents, system__find_graphs]
related: [knowledge/the-platform-wiki, toolsets/list-and-describe-tools]
---
## Goal

Go from a task in your own words ("review this repository without me watching") to the page that explains it, the exact call to make, and the agents and graphs this install already has for it.

## Prerequisites

- Nothing to create. Every call here only reads.
- `system__wiki_search` and `system__wiki_read` are in the core kit: a seeded agent calls them directly, as below. An agent without the kit calls them through `system__call_tool` (`"toolset_id": "system"`).

## Steps

### 1. Search the wiki

Search with a few distinctive words of the task with `system__wiki_search`:

```json
{"name": "system__wiki_search", "arguments": {"query": "start session workspace brief", "limit": 5}}
```

Response:

```json
{"hits": [{"path": "sessions/start-a-session", "excerpt": "...start a session: one agent or graph working inside a workspace on the brief you give it...", "score": 0.61}, {"path": "cookbook/create-and-run-a-session", "excerpt": "...", "score": 0.44}], "truncated": false, "mode_used": "fulltext"}
```

Each hit is a page: a feature page, a recipe or a tool's reference page. `mode_used` says which rung answered. Take the best `path` into the next step.

### 2. Read the page

Read the whole page with `system__wiki_read`:

```json
{"name": "system__wiki_read", "arguments": {"path": "sessions/start-a-session"}}
```

Response:

```json
{"path": "sessions/start-a-session", "title": "Start a session", "summary": "Start an agent or graph in a workspace with a brief, and decide whether it runs at once.", "kind": "guide", "body": "## What it is\n...", "children": []}
```

The body says when to use the tool, in what order, and what goes wrong. Every tool it names links its reference page, `reference/tools/<toolset>/<tool>`.

### 3. Read the tool's reference page

Read the exact call with `system__wiki_read` on the reference path:

```json
{"name": "system__wiki_read", "arguments": {"path": "reference/tools/workspaces/create_workspace_session"}}
```

Response:

```json
{"path": "reference/tools/workspaces/create_workspace_session", "title": "workspaces__create_workspace_session", "kind": "reference", "body": "# workspaces__create_workspace_session\n\n## What it does\n...\n## Call it\n...", "children": []}
```

A reference page is generated from the tool's own descriptor: what it does, a validating call, the argument schema, its facts (role, default approval, whether it pauses the turn or needs a workspace), examples and the pages that use it.

### 4. Describe the tool

Ask for the live facts with `system__describe_tool`:

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "workspaces", "tool_name": "create_workspace_session"}}
```

Response (abridged):

```json
{"scoped_id": "workspaces__create_workspace_session", "required_role": "user", "pauses_turn": false, "needs_workspace": false, "approval": {"gated": false}, "reference": "reference/tools/workspaces/create_workspace_session", "call": {"via": "call_tool"}}
```

`args_schema` is the exact schema, and `call.example` holds the arguments to send. `call.via` is `call_tool` for a toolset's tool and `direct` for the session file tools and `client__open_file`. `approval` is the gate on the tool now, not the one the install shipped with. `required_role` is the role the tool needs: `admin` means it needs an admin, and a lower run's call is refused with `forbidden`.

### 5. Look for an agent that does the job

Before you build an agent, find one with `system__find_agents`. This matches a description that mentions review, in any case:

```json
{"toolset_id": "system", "tool_name": "find_agents", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "description"}, "op": "~=*", "right": {"kind": "value", "value": "%review%"}}, "limit": 10}}
```

Response:

```json
{"kind": "offset", "items": [{"id": "code-reviewer", "description": "Reviews diffs", "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}}], "offset": 0, "length": 1, "total": 1}
```

`~=*` is a case-insensitive pattern match, with `%` for any run of characters. Each item is the full agent row.

### 6. Look for a graph that does the job

Find a multi-step workflow with `system__find_graphs`:

```json
{"toolset_id": "system", "tool_name": "find_graphs", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "description"}, "op": "~=*", "right": {"kind": "value", "value": "%review%"}}, "limit": 10}}
```

Response:

```json
{"kind": "offset", "items": [], "offset": 0, "length": 0, "total": 0}
```

No graph matches, so run the `code-reviewer` agent in a session ([create and run a session](cookbook/create-and-run-a-session)).

## Verify

You hold four things: the page that explains the task, the scoped id (`workspaces__create_workspace_session`), arguments that match `args_schema`, and a `required_role` your run clears. You also know whether an agent or graph for the job exists here.

## Gotchas

- A hit is a page, not an answer. The excerpt is a few words around the match: read the page before you act.
- Until an operator turns search on for the wiki, `auto` answers from the fulltext rung, and an explicit `"mode": "semantic"` is refused. Fulltext matches only the pages that hold every word of the query, so a long sentence often finds nothing. Search with two to four distinctive words, and try other words before you give up.
- The live map of this install (`this-install/`) is left out of a search unless `path_prefix` names it. `"path_prefix": "this-install/agents/"` searches the agents of this install by their descriptions.
- Prefer an existing agent or graph to a new one. Creating one is approval-gated by default, so it waits for a person's approval.
- Describing a tool does not grant it. When your agent holds `system__call_tool`, a tool outside its `tools` list is still reachable through it, at your run's role.
- A tool name `system__describe_tool` does not know answers `not-found` and lists the toolset's tools: pick the name from that list.

## Related

- [The platform wiki](knowledge/the-platform-wiki)
- [List and describe tools](toolsets/list-and-describe-tools)
