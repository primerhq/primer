---
title: "Create and edit agents"
summary: "Create, list, get, find, update and delete agent rows, with the field checks, the seeded roster and the reset route."
order: 2
tools: [crud__create_agent, crud__update_agent, system__list_agents, system__get_agent, system__create_agent, system__update_agent, system__delete_agent, system__find_agents]
related: [agents/prompts-and-parts, agents/tools-and-grants, providers/model-profiles]
---
## What it is

An agent row is created and edited through the `crud` and `system` agent tools, or through the REST routes `POST /v1/agents`, `GET /v1/agents`, `GET /v1/agents/{entity_id}`, `PUT /v1/agents/{entity_id}`, `DELETE /v1/agents/{entity_id}` and `POST /v1/agents/find`. A create or update carries the whole row; the runtime never patches. Before the write, the same semantic checks run as on the REST route: the id pattern, the description, the model profile and the toolsets.

## Concepts

### The row, field by field

- `id` - the stable identifier. Optional on create: omitted, the server generates `agent-<hex>`. Sent, it must match `^[a-z0-9][a-z0-9_-]{0,62}$` - lowercase letters, digits, hyphens and single underscores, 63 characters at most, never `__`. It cannot change afterwards.
- `description` - free text, embedded for search. Must not be blank.
- `access` - who owns the agent and who it is shared with: `owners`, people granted `read` or `full`, and whether it is `public`. The server sets it on create (the creator owns the agent) and ignores it in create and update bodies; change it with `system__set_entity_access`, owners only.
- `model` - `{profile_id}` naming a stored model profile, which carries the provider and the model.
- `temperature` - optional sampling temperature; `null` defers to the provider's default.
- `max_tool_turns` - cap on tool-call rounds within one turn; default 50; `null` is unbounded.
- `max_output_tokens` - cap on tokens per completion; `null` is unbounded.
- `tools` - a list of scoped tool ids, `<toolset_id>__<tool_name>`; the runtime exposes exactly the listed tools. An empty list is an agent with no tools.
- `system_prompt` - a list of strings, joined and rendered against the run's execution context. See [Prompt parts and the execution context](agents/prompts-and-parts).
- `compaction_prompt` - optional list of strings steering how the session compacts this agent's history; empty falls back to the default.
- `compaction_tool_access` - whether the compaction's summariser may call tools while it works.
- `allow_external_tools` - whether the agent may call tools the invoker provides from outside the session.
- `use_workspace_instructions` - whether the agent reads the workspace's `agents.md` instructions; default `true`.
- `response_format` - a JSON schema the final message must match; the session's persistent response format beats the agent's.
- `tts_voice` - the voice spoken TTS uses for the final message.
- `harness_id` - set only by a harness install. A non-null row is managed: public CRUD cannot create with it set or modify it.

### The create checks

A create refusing one of the checks answers a `validation-error` that names the field: `agent_id_invalid` at `id`, `agent_description_blank` at `description`, `model_profile_not_found` at `model.profile_id`, `toolset_not_found` at `tools` (every missing toolset named once). A body that sets `harness_id` is refused with `managed_field_set`. A duplicate id is a `conflict`.

### The update is a replace

`system__update_agent` and `PUT /v1/agents/{entity_id}` take the whole row: the body `id` must equal the path id, and an unknown id is a `not-found`. An update re-checks only what it adds or changes: the profile is checked only when it changes, and only the toolsets the update adds are checked, so a row that names a toolset deleted later is still editable. The id cannot change. A secret field a `get` served masked, sent back unchanged, keeps its stored value (for the same origin: scheme, host and port).

### The seeded roster

Five agents seed an install, each with the [core kit](start-here/the-core-kit): `operator` (the front door of the install: it answers, delegates and conducts, and alone of the five also holds `workspaces__list_my_sessions` and `workspaces__message_session`), `builder` (builds and edits platform objects), `planner` (turns a task into a stepwise plan), `explorer` (finds what exists on the platform for a topic) and `tool-runner` (finds and runs the right tool for one self-contained request). `POST /v1/setup/reset_agents` re-applies the default prompt, grants and description of exactly those rows, preserving the `model.profile_id` each row runs under.

### Choosing a model profile

A principal that is not an admin sees the user-tier model profiles from `GET /v1/model_profiles/choices` (id, description, kind, context length, sorted by id), and the agent form offers that list. An admin manages profiles through `POST /v1/model_profiles`, `GET /v1/model_profiles/{entity_id}` and their siblings. See [Model profiles](providers/model-profiles).

### The status check

`GET /v1/agents/{agent_id}/status` returns `{ok, issues, warnings}`: the references the row cannot resolve (the profile, the toolsets) without calling the live providers. An output cap that fills the model's context window is a warning, not an issue.

## How to

1. Pick a profile from `GET /v1/model_profiles/choices` (an admin creates or names a profile first).
2. Create the row with `system__create_agent` (or `POST /v1/agents`) and a full entity body.
3. Verify it with `system__get_agent` and `GET /v1/agents/{agent_id}/status`; `ok` true means every reference resolves.
4. Edit it with `system__update_agent` (or `PUT /v1/agents/{entity_id}`) and the whole row, including the unchanged fields.
5. Remove it with `system__delete_agent` (or `DELETE /v1/agents/{entity_id}`) once nothing references it.

## Tool calls

### crud__create_agent

```json
{"toolset_id": "crud", "tool_name": "create_agent", "arguments": {"entity": {"id": "code-reviewer", "description": "Reviews diffs", "model": {"profile_id": "my-profile"}}}}
```

Reference: [`crud__create_agent`](reference/tools/crud/create_agent).

### crud__update_agent

```json
{"toolset_id": "crud", "tool_name": "update_agent", "arguments": {"id": "code-reviewer", "entity": {"id": "code-reviewer", "description": "Reviews diffs and posts findings", "model": {"profile_id": "my-profile"}}}}
```

Reference: [`crud__update_agent`](reference/tools/crud/update_agent).

### system__list_agents

```json
{"toolset_id": "system", "tool_name": "list_agents", "arguments": {"offset": 0, "limit": 20}}
```

Reference: [`system__list_agents`](reference/tools/system/list_agents).

### system__get_agent

```json
{"toolset_id": "system", "tool_name": "get_agent", "arguments": {"id": "code-reviewer"}}
```

Reference: [`system__get_agent`](reference/tools/system/get_agent).

### system__create_agent

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "code-reviewer", "description": "Reviews diffs", "model": {"profile_id": "my-profile"}}}}
```

Reference: [`system__create_agent`](reference/tools/system/create_agent).

### system__update_agent

```json
{"toolset_id": "system", "tool_name": "update_agent", "arguments": {"id": "code-reviewer", "entity": {"id": "code-reviewer", "description": "Reviews diffs and posts findings", "model": {"profile_id": "my-profile"}}}}
```

Reference: [`system__update_agent`](reference/tools/system/update_agent).

### system__delete_agent

```json
{"toolset_id": "system", "tool_name": "delete_agent", "arguments": {"id": "code-reviewer"}}
```

Reference: [`system__delete_agent`](reference/tools/system/delete_agent).

### system__find_agents

```json
{"toolset_id": "system", "tool_name": "find_agents", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "id"}, "op": "=", "right": {"kind": "value", "value": "code-reviewer"}}}}
```

Reference: [`system__find_agents`](reference/tools/system/find_agents).

## Gotchas

- An update is a replace, not a patch. Send the whole row; a field you leave out is gone, not preserved.
- The id is immutable, and the pattern is checked on create only. A row stored under an older id keeps working.
- `agent_description_blank` fires on create and on an update that MAKES the description blank: other agents find an agent by its description, so an empty one is refused.
- A tool id with no `__` is checked as a toolset name but matches no tool, so it never provides one. Always write `<toolset_id>__<tool_name>`.
- A tool id naming the `canvas` toolset resolves while the canvas setting `agent_tools_enabled` is on, which it is by default now that the canvas panel ships; switched off, a `canvas` grant is refused like a toolset that does not exist.
- Deleting an agent that a graph agent node, an open session or a trigger subscription still targets is refused with `type=conflict` and the `in_use_by` list. Detach them first.
- Deleting the seeded `operator` or `builder` marks the install as not set up: admins see the Finish setup screen from their next page load and every other user waits on a setup screen until the agent is back. `POST /v1/setup/seed` (or the next boot) re-seeds.
- A `harness_id` row is managed by its harness: create with `harness_id` set is refused (`managed_field_set`), and update and delete are refused with `managed_entity`. Sync the harness instead of editing the row.

## Related

[Prompt parts and the execution context](agents/prompts-and-parts), [Tool grants and the rank rule](agents/tools-and-grants), [Model profiles](providers/model-profiles).
