---
title: "Sharing and access"
summary: "Who may read, change, share or delete an agent, graph, toolset, template, channel, collection, trigger or harness, and how to share one."
tools: [system__get_entity_access, system__set_entity_access, system__lookup_users]
related: [start-here/roles-and-approvals, agents/create-and-edit]
order: 60
---
## What it is

Agents, graphs, toolsets, workspace templates, channels, collections, triggers and harnesses belong to the users who own them. Each one is private until an owner shares it with chosen users or makes it public. These tools read and change that sharing.

## Concepts

- **Owners.** The user who creates an entity owns it. An entity can have several owners. Only an owner can change its sharing, add or replace owners, or delete it.
- **Levels.** Sharing grants a level: `read` (see it, run it, use its tools) or `full` (also edit it). `owner` is the third level and is held only by owners.
- **Public.** A public entity is readable by every full user, at `read`. A toolset can instead mark single tools public, which lets everyone use those tools without exposing the toolset itself.
- **Admins** hold `owner` on everything. A `restricted` account reaches nothing.
- **Your runs act as you.** An agent never brings access of its own: what a run can read or change is what the person behind it can.
- **Not found means not yours.** An entity you cannot read answers exactly like one that does not exist.
- **Some things share with their parent.** A document has the access of its collection, a subscription (or channel binding) the access of its trigger, and what a harness installed the access of the harness. Share the parent, never the child: sharing an installed row directly answers `type=conflict` (`harness_managed`).

## What each level allows

| Kind | read | full | owner |
|---|---|---|---|
| Collection | list, read, grep and search its documents | write, move, delete and import documents | delete it, share it, turn search on or off |
| Trigger | see it and its subscriptions, park on it | edit it, fire it now, manage its subscriptions and channel bindings | delete it, share it, rotate its webhook token |
| Harness | see it and what it installed | (harness writes are admin-only for now) | share it |

The platform wiki (the `system` collection) is public and read-only for everyone. A trigger on a channel needs `read` on that channel when you save it, and a trigger without a channel is admin-only. A trigger still runs as whoever saved it last.

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

Share a collection with a teammate who may edit its documents:

```json
{"toolset_id": "system", "tool_name": "set_entity_access", "arguments": {"kind": "collection", "id": "kb-onboarding", "owners": ["user-1"], "grants": {"user-2": "full"}, "rev": 0}}
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
- A collection's search settings change only through its search routes, by an owner: replacing the whole collection with a different `search` block is refused.

## Related

[Roles and approvals](start-here/roles-and-approvals), [Create and edit agents](agents/create-and-edit).
