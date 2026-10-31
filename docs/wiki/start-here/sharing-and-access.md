---
title: "Sharing and access"
summary: "Who may read, change, share or delete an agent, graph, toolset, workspace template or channel, and how to share one."
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
- **Sign in for yourself.** An MCP toolset that signs in through the browser keeps one sign-in per person. If it is shared with you, or one of its tools is public, you connect your own sign-in in the console (Toolsets, the toolset, Connect: "Your sign-in"), and your runs send only your token, never the owner's. A run whose person has not signed in is told to sign in. A toolset that signs in with client credentials has one shared sign-in ("Shared sign-in") that only people with full access can connect. `system__get_toolset_auth_status` reads your own status.
- **What an MCP client sees.** A client connected to primer's MCP endpoint acts as the person behind its token and is offered only primer's built-in tools, never a tool of a toolset someone registered. Those tools check that person's access like any run does.
- **The activity log names the person.** Every change records the user behind it (for an API token, its owner), so an admin's activity view shows who did what.

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
- Removing someone's access to an MCP toolset does not delete their sign-in, but their runs stop using it at once, and a sign-in they started before losing access connects nothing. Changing the toolset's URL or sign-in settings, or deleting it, forgets every person's sign-in.

## Related

[Roles and approvals](start-here/roles-and-approvals), [Create and edit agents](agents/create-and-edit).
