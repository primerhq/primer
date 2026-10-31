---
title: "Sharing and access"
summary: "Who may read, change, share or delete an agent, graph, toolset, template or channel, how to share one, and who may use models, providers and tools."
tools: [system__get_entity_access, system__set_entity_access, system__lookup_users]
related: [start-here/roles-and-approvals, agents/create-and-edit]
order: 60
---
## What it is

Agents, graphs, toolsets, workspace templates and channels belong to the users who own them. Each one is private until an owner shares it with chosen users or makes it public. These tools read and change that sharing.

## Concepts

- **Owners.** The user who creates an entity owns it. An entity can have several owners. Only an owner can change its sharing, add or replace owners, or delete it.
- **Levels.** Sharing grants a level: `read` (see it, run it, use its tools) or `full` (also edit it). `owner` is the third level and is held only by owners.
- **Public.** A public entity is readable by every full user, at `read`. A toolset can instead mark single tools public, which lets everyone use those tools without exposing the toolset itself.
- **Admins** hold `owner` on everything. A `restricted` account reaches nothing.
- **Your runs act as you.** An agent never brings access of its own: what a run can read or change is what the person behind it can.
- **Not found means not yours.** An entity you cannot read answers exactly like one that does not exist.

## Who can use

Some things are not shared like your own entities: an admin decides who may **use** them.

- **Models and providers.** A model profile and the providers behind your agents (the LLM provider, workspace providers, channel providers and the embedding, reranking and vector store providers a collection's search uses) carry a use grant. A new one is private to admins until an admin grants use to chosen users or makes it public. You can name a profile in an agent or graph only if you may use it; one you may not use answers as if it did not exist.
- **A run uses them as the person it runs as.** If an agent runs for someone who may not use its profile, the turn does not run: the session pauses with the reason `model profile 'X' is not available to the person this turn runs as` (`model_profile_unusable`), and resumes once an admin grants use. Nothing is lost.
- **Tools follow the same person.** A run only gets the tools its person may use: a toolset you cannot read is left out and the model is told `not available to you`, unless its owner marked single tools public, in which case just those tools are offered (`some tools not available to you`).
- **Global settings.** Settings every feature already uses (web search and fetch, speech, artifact storage, canvas, MCP exposure) can be read by every user with secrets masked. Only an admin changes them.

## How to

1. Read the current sharing and note its `rev`.
2. Find the user ids you need by username with `lookup_users`.
3. Send the whole desired state (owners, grants, public, public tools) with the `rev` you read. A stale `rev` answers `type=conflict`: read again and retry.

## Tool calls

Read who an agent is shared with:

```json
{"toolset_id": "system", "tool_name": "get_entity_access", "arguments": {"kind": "agent", "id": "agent-3f9a1c8d"}}
```

Find a user to share with:

```json
{"toolset_id": "system", "tool_name": "lookup_users", "arguments": {"q": "ali"}}
```

Share the agent with that user at `read`, keeping yourself as owner:

```json
{"toolset_id": "system", "tool_name": "set_entity_access", "arguments": {"kind": "agent", "id": "agent-3f9a1c8d", "owners": ["user-1"], "grants": {"user-2": "read"}, "rev": 0}}
```

Make two tools of a toolset public without sharing the rest:

```json
{"toolset_id": "system", "tool_name": "set_entity_access", "arguments": {"kind": "toolset", "id": "toolset-9", "owners": ["user-1"], "public_tools": ["lookup", "summarise"], "rev": 2}}
```

## Gotchas

- `set_entity_access` replaces the whole sharing state. Leave someone out of `grants` and they lose access.
- An entity always keeps at least one owner. To hand one over, list the new owner and, if you still want access, give yourself a grant.
- `access` in a create or update body is ignored: sharing changes only through `set_entity_access`.
- Only enabled full users can be owners or grantees; a restricted or disabled account is refused.
- Profiles and providers are shared by use grants only: only an admin sets them, with `set_entity_access` kinds `model_profile`, `llm_provider`, `embedding_provider`, `cross_encoder_provider`, `semantic_search_provider`, `workspace_provider` and `channel_provider` (no owners, every grant `read`). You can read the sharing of one you may use, but it lists no one else.

## Related

[Roles and approvals](start-here/roles-and-approvals), [Create and edit agents](agents/create-and-edit).
