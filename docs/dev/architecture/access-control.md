# Access Control

## 1. Purpose

Access control decides which user may read, change, share or delete an entity. Every user-created entity of an owned
kind carries an `access` block on its own row: its owners, the users it is shared with at a level, and whether it is
public. One package, `primer/access/`, turns a caller into a person and decides levels; every REST route, system tool
and MCP call that touches an owned entity goes through it, so the doors cannot disagree.

There are three levels, the same for every owned kind: `read`, `full` and `owner`. Public always means `read` for every
full user. Admins (and the system principal of an auth-disabled install) hold `owner` on everything; a `restricted`
account (the onboarding gate) holds nothing. An agent or graph never brings access of its own: what a run may reach is
decided by the person behind it.

The owned kinds are agents, graphs, toolsets, workspace templates and channels. Workspaces, sessions, collections,
triggers, harnesses and the grantable infrastructure (providers and model profiles) follow the same model in later
phases.

## 2. Visual overview

```mermaid
flowchart LR
    req[REST request] --> pr[person_from_request]
    tool[system or crud tool] --> pt[person_for_tool]
    mcp[MCP call] -->|current_actor| pt
    pr --> person[Person user_id + role]
    pt --> person
    person --> pol[policy: level_of / require / visible_predicate]
    row[(entity row: access block)] --> pol
    pol -->|single row| verdict[level or 404 or 403]
    pol -->|lists| pred[members CONTAINS u:id OR public]
    pred --> store[(storage find, GIN index on access.members)]
    svc[access service: get_access_view / set_access / lookup_users] --> pol
```

## 3. Public surface

- `primer/model/access.py`: `AccessLevel` (`read`, `full`, `owner`) and `LEVEL_RANK`; `Access` (`owners`, `grants`,
  `public`, `public_tools`, the derived `members` tokens and the `rev` counter) with `Access.owned_by(user_id)`; the
  `Owned` mixin that adds `access` to a model.
- `primer/access/person.py`: `Person` (`user_id`, `role`, `is_admin`, `is_full_user`); `person_from_request` (an API
  token acts as its owner user), `person_from_ref` (a persisted `PrincipalRef`) and `person_for_tool` (the run's
  initiator when a tool has a `ToolContext`, else the MCP endpoint's `current_actor`).
- `primer/access/policy.py`: `level_of`, `require` (no level raises `NotFoundError`, too low a level raises
  `AccessDeniedError`, rendered as 403 with code `access_denied`), `visible_predicate` (the list filter; `None` for
  admins), `readable_ids` (the ids of the rows a person holds a level on, for an inherited kind's `<parent>_id IN`
  filter; `None` for admins) and `tool_is_public`.
- `primer/access/service.py`: `get_access_view`, `set_access` (owners only; the whole desired state; refuses an empty
  owner list, unknown, disabled or restricted users, and per-tool flags on kinds without tools; writes the `access`
  block alone with `patch_if` guarded on `access.rev`) and `lookup_users` (username prefix, id and username only).
- REST: `make_crud_router(..., owned=True)` (`primer/api/routers/_crud.py`), or `row_guard` plus `list_filter` for a
  kind that inherits its parent's access (documents from their collection), and `make_access_router(...)`
  (`primer/api/routers/access.py`): `GET` and `PUT /v1/{plural}/{id}/access`, plus `GET /v1/users/lookup`.
- Tools: `system__get_entity_access`, `system__set_entity_access`, `system__lookup_users`
  (`primer/toolset/_system_access.py`); the generated CRUD tools enforce the same levels.

## 4. How to add a new implementation

To make another entity kind owned:

1. Mix `Owned` into its model (`class Thing(Owned, Identifiable)`).
2. Add `("access_members", False, "USING GIN ((data->'access'->'members'))")` for its table to `_HOT_FIELD_INDEXES` in
   `primer/storage/postgres.py`.
3. Pass `owned=True` to its `make_crud_router` and include `make_access_router(...)` for it BEFORE the CRUD router (the
   route-order rule of `_crud.py`). Every other route that reads the row calls `require(person_from_request(request),
   row.access, <level>, what=...)`.
4. Give its tools the same rules: `_crud_tools_for(..., owned=True)` for generated CRUD tools, `person_for_tool(ctx)`
   plus `require` / `visible_predicate` for hand-written ones, and add the kind to `_KINDS` in
   `primer/toolset/_system_access.py`.
5. Add a forward-only migration that gives existing rows an owner, the way migration 21 does.

## 5. Existing implementations

| Kind | Router | Tools | Notes |
|---|---|---|---|
| Agent | `primer/api/routers/compute.py` | system CRUD, `crud` toolset | seeded agents are public with no owner |
| Graph | `primer/api/routers/compute.py` | system CRUD, `crud` toolset | |
| Toolset | `primer/api/routers/providers.py` | system CRUD, `crud` toolset | the only kind with `public_tools` |
| Workspace template | `primer/api/routers/workspaces.py` | `workspaces` toolset | `local-default` is public with no owner |
| Channel | `primer/api/routers/channels.py` | system CRUD | |

## 6. Wiring

The auth middleware sets `user_id` on every `Principal` (the user on the cookie path, the token's owner on the bearer
path, `None` for the system principal); `PrincipalRef.from_principal` keeps it on persisted refs. Routers read the
person per request; tools read it from the `ToolContext` or, over MCP, from `primer.mcp.server.current_actor`. The list
filter compiles to `data->'access'->'members' ? $n` on Postgres, served by the GIN index; SQLite installs scan.
Migration 21 (`primer/storage/migrations/m021_entity_ownership.py`) stamps the first admin as owner of rows created
before access control, and makes the seeded agents and template public.

### Run identity

Every run carries a composite identity: the person behind it plus the entity acting. The person is
`ToolContext.initiated_by` (the turn person of a steered turn, a token's owner for an API-token run); the actor is
`ToolContext.actor`, an `ActorRef` (`primer/model/principal.py`, `kind` one of `agent`, `graph`, `user`, `system`, plus
`id`). The actor is the innermost acting entity: the session's agent, a graph agent node's agent, a sub-agent, a ToolCall
node's graph or a child graph. A direct MCP call acts as the user (`system` on an auth-disabled install). The builders
set it on the `ToolExecutionManager` (`actor=`; default: the bound session's agent), which stamps it on every context.
The actor never adds access: every decision is still the person's.

`identity_doc(ref, actor, username=None)` (`primer/access/person.py`) renders it; `gate_identity` reads an API token
owner's username from the `User` row when a gate fires, and `tool_identity(ctx, storage_provider)` reads it the way
`person_for_tool` reads the person. Every gate that builds an approval input passes it as `ApprovalContext.identity`
(the tool manager's gate, `system__call_tool` with and without a context, the MCP endpoint), so the input always has an
`identity` key:

```json
{"user": {"id": "u-7", "name": "bob", "role": "user"}, "actor": {"kind": "agent", "id": "agent-builder"}}
```

## 7. Testing patterns

- `tests/access/`: `Person` derivations and the policy, including a matrix that pins list visibility to `level_of` for
  every row and person (`test_list_and_get_agree_on_every_row_and_person`).
- `tests/api/test_crud_owned.py` and `tests/api/test_access_routes.py`: a minimal app with a test-only middleware that
  turns an `X-Test-User: <id>:<role>` header into `request.state.actor`, on a real SQLite provider.
- `tests/toolset/test_system_access_tools.py`: tool refusals and the MCP path (set and reset `current_actor`).
- `tests/storage/test_access_index.py`: the GIN index and its use by the list query (Postgres lane).
- `tests/storage/test_m021_entity_ownership.py`: the migration rules and the public seeded rows.

## 8. Historical decisions

- Access lives on the row rather than in a side table: one write changes it atomically, the CRUD event carries it, and
  the list filter is a single indexed predicate with no join.
- `public_tools` alone does not make a toolset listable: listing and reading must agree, and single public tools are
  reached through the tool catalogue, not the toolset row.
- The sessions-are-a-shared-read decision (G7-02) is reversed by this model; sessions follow their workspace once
  workspaces are owned.
