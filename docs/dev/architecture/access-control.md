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

The owned kinds are agents, graphs, toolsets, workspace templates, channels, workspaces, sessions (inherited from their workspace),
collections, triggers and harnesses. Three kinds have no access of their own and inherit a parent's: a document inherits its collection's
and a trigger subscription its trigger's (both by CHECKING the parent on every door), and an agent, graph, toolset or collection a
harness installed carries a COPY of the harness's access, rewritten whenever the harness is shared (6.12). Model profiles and the
providers a user-owned entity references are not owned but grantable: an admin-set use grant says who may USE them (section
"Grantable infrastructure").

## 2. Visual overview

```mermaid
flowchart LR
    req[REST request] --> pr[person_from_request]
    req -->|auth middleware| acting[acting person]
    run[tool manager: a run's listing and calls] -->|initiated_by| acting
    tool[system or crud tool] --> pt[person_for_tool]
    acting --> pt
    mcp[MCP call] -->|current_actor| pt
    acting --> ev[event actor user:id]
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
  initiator when a tool has a `ToolContext`, else the acting person, else the MCP endpoint's `current_actor`).
- `primer/access/acting.py` (a leaf module: it imports nothing from primer at runtime): `acting_as(person)` (a context
  manager over one ContextVar, reset on exit), `acting_person()` and `actor_label(person)` (`user:<id>`, else
  `system`).
- `primer/access/tools.py`: `toolset_use` (which tools of which toolsets a person may use) and `may_use_toolset` (at
  least one tool: who may sign in to an MCP toolset and read its status).
- `primer/access/policy.py`: `level_of`, `require` (no level raises `NotFoundError`, too low a level raises
  `AccessDeniedError`, rendered as 403 with code `access_denied`), `visible_predicate` (the list filter; `None` for
  admins), `readable_ids` (the ids of the rows a person holds a level on, for an inherited kind's `<parent>_id IN`
  filter; `None` for admins) and `tool_is_public`.
- `primer/access/service.py`: `get_access_view`, `set_access` (owners only; the whole desired state; refuses an empty
  owner list, unknown, disabled or restricted users, per-tool flags on kinds without tools, and a row a harness
  installed with 409 `harness_managed`; writes the `access` block alone with `patch_if` guarded on `access.rev`, then
  runs an optional `after_write` hook), `lookup_users` (username prefix, id and username only), and the two writers
  that keep the sharing out of a whole-row write: `update_keeping_access` (every other field, fenced on the rev the
  caller read: a sharing change in between answers 409) and `rewrite_keeping_access` (every other field, fenced on the
  rev read fresh and retried, for long operations such as a harness sync whose snapshot may be stale).
- Field-scoped platform writers: `primer/knowledge/row_writes.py` `write_collection_fields` and
  `primer/trigger/row_writes.py` `write_trigger_fields` (one `patch_if` of the named fields only).
- Inherited kinds: `primer/toolset/_collection_access.py` (`collection_for`: the tool twin of the REST
  `_require_collection`; `readable_documents`: the document-list filter), the trigger service gate
  `primer/trigger/service.py` `_require_trigger` (every function that takes `person`), `primer/harness/access.py`
  `apply_harness_access` (the fan-out of a harness's access to what it installed) and
  `primer/trigger/channel_access.py` `require_channel_trigger_access` (a channel trigger needs read on its channel).
- REST: `make_crud_router(..., owned=True)` (`primer/api/routers/_crud.py`), or `row_guard` plus `list_filter` for a
  kind that inherits its parent's access (documents from their collection), and `make_access_router(...)`
  (`primer/api/routers/access.py`): `GET` and `PUT /v1/{plural}/{id}/access`, plus `GET /v1/users/lookup`.
- Tools: `system__get_entity_access`, `system__set_entity_access`, `system__lookup_users`
  (`primer/toolset/_system_access.py`); the generated CRUD tools enforce the same levels.
- Phase 4: `primer/model/access.py` `Grantable` (the mixin of grantable infrastructure) and `TOOLS_PUBLIC_TOKEN`;
  `primer/access/policy.py` `may_use_tool` and `catalogue_predicate`; `primer/access/tools.py` `toolset_use`;
  `primer/access/infra.py` `usable`; `set_access(use_only=True)` and `get_access_view(use_only=True)`;
  `primer/api/routers/infra_access.py`; `primer/authz.py` `ProfileUnusable`; `primer/model_profile/resolver.py`
  `require_profile_use` and `resolve_llm(person=)`; `primer/workspace/provider_use.py`; `primer/channel/checks.py`
  `channel_for_binding`; `primer/knowledge/search_use.py`.

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

A kind that inherits instead checks its parent on every door (documents: `row_guard` / `list_filter` and
`collection_for`; subscriptions: the `person` argument of every trigger service function) or carries a copy that the
parent's `set_access` rewrites (harness contents: `after_write=apply_harness_access`). Never widen a child beyond its
parent.

## 5. Existing implementations

| Kind | Router | Tools | Notes |
|---|---|---|---|
| Agent | `primer/api/routers/compute.py` | system CRUD, `crud` toolset | seeded agents are public with no owner |
| Graph | `primer/api/routers/compute.py` | system CRUD, `crud` toolset | |
| Toolset | `primer/api/routers/providers.py` | system CRUD, `crud` toolset | the only kind with `public_tools` |
| Workspace template | `primer/api/routers/workspaces.py` | `workspaces` toolset | `local-default` is public with no owner |
| Channel | `primer/api/routers/channels.py` | system CRUD | |
| Collection | `primer/api/routers/knowledge.py` | `collections` toolset, system CRUD and collection extras, the wiki tools | `system` is public and write-protected; `_internal_*` are admin-only; the search block changes only through `/search` (6.8, 6.9) |
| Document (inherited) | `knowledge.py` (`/collections/{id}/...` and the flat `/v1/documents`) | `collections` toolset, system document tools, generic `get_document` / `find_documents` | inherits its collection by check (6.8) |
| Trigger | `primer/api/routers/triggers.py` | `trigger` toolset (re-exported by `crud`), system channel-binding tools | run-as unchanged; the gate is `_require_trigger` (6.10) |
| Subscription (inherited) | `triggers.py` (`/triggers/{id}/subscriptions...`) | `trigger` toolset, `create/list/delete_channel_binding`, `subscribe_to_trigger`, `subscribe_to_channel_event` | inherits its trigger by check (6.10) |
| Harness | `primer/api/routers/harness.py` | `harness` toolset | writes stay admin-only; list, get and bundle need read (6.12) |
| Harness-installed rows (inherited) | the owning kinds' routers | the owning kinds' tools | carry a copy of the harness's access; direct sharing answers 409 `harness_managed` (6.12) |
| Model profile (grantable) | `primer/api/routers/model_profiles.py` | system CRUD | use grant; the choices list is filtered |
| LLM provider (grantable) | `primer/api/routers/providers.py` | system CRUD | use grant, checked with the profile that names it |
| Embedding, cross-encoder, vector store provider (grantable) | `providers.py`, `semantic_search.py` | system CRUD | use grant, checked when search is enabled and at every search |
| Workspace provider (grantable) | `primer/api/routers/workspaces.py` | `workspaces` toolset | use grant, checked at workspace create and template save; choices filtered |
| Channel provider (grantable) | `primer/api/routers/channels.py` | system CRUD | use grant, checked when a channel is saved or bound |
| MCP OAuth sign-in | `primer/api/routers/mcp_oauth.py` | `system__get_toolset_auth_status` | not an owned kind: a browser sign-in is keyed by (toolset, user) and is its person's own; client credentials stay one shared row per toolset (see [mcp-oauth](../subsystems/mcp-oauth.md)) |
| MCP server catalogue | `primer/mcp/dispatch.py` | the exposed built-in tools | only reserved built-in toolsets are exposable (`is_exposable`, `not_system_toolset`), so no owned toolset reaches an MCP client; the built-in tools check the caller's access |
| Event log actor | `primer/storage/{postgres,sqlite}.py`, `primer/events/recorder.py` | every tool that writes | `user:<id>` of the acting person, else `system` |

## 6. Wiring

The auth middleware sets `user_id` on every `Principal` (the user on the cookie path, the token's owner on the bearer
path, `None` for the system principal); `PrincipalRef.from_principal` keeps it on persisted refs. Routers read the
person per request; tools read it from the `ToolContext` or, over MCP, from `primer.mcp.server.current_actor`. The list
filter compiles to `data->'access'->'members' ? $n` on Postgres, served by the GIN index; SQLite installs scan.
Migration 21 (`primer/storage/migrations/m021_entity_ownership.py`) stamps the first admin as owner of rows created
before access control, and makes the seeded agents and template public. Migration 26
(`primer/storage/migrations/m026_infrastructure_public.py`) makes every existing model profile and provider with no
sharing public, on the seven grantable tables (a row that already carries grants or `public` is left alone, so a re-run
never undoes an admin's choice).

### The acting person

`primer/access/acting.py` holds who the current code acts for. The auth middleware sets it for every authenticated
request, REST, WebSocket and MCP alike: the user, the owner of a bearer token (never the token), and the system person
when auth is disabled; an unauthenticated request sets nothing, and the person is reset when the request ends. The tool
manager runs every listing and every call of a run under `acting_as(person_from_ref(initiated_by))`, so a run whose
task was spawned from another user's request acts for its own person; `dispatch_inner_tool` does the same from its
context (an approved re-run), and the MCP task bridge polls a parked task call as its session's `turn_person`, else
its `initiated_by`. Two things read it: `person_for_tool` with no `ToolContext` (so an MCP OAuth listing sends the
acting person's token) and the event writers. Worker internals, trigger dispatch and the dispatcher act for nobody.

### MCP OAuth per person

A browser (authorization-code) MCP sign-in belongs to one person: its token row is keyed by
`token_key(toolset_id, user_id)` and names both, and a call or listing sends only the token of
`person_for_tool(ctx)`. Nobody to act for (an unattributed run) means no browser token, never another row. A
client-credentials sign-in is one shared row per toolset (the bare toolset id), and so is any sign-in of the system
person. Any full user who may use at least one tool of the toolset (`may_use_toolset`: a level on it, or a public tool)
starts, reads and ends their own browser sign-in; client credentials need FULL to connect or disconnect and use to
read. A toolset the caller may not use answers like a missing one on every route and in the status tool. The callback
writes the starter's row only if that user still exists, is enabled and full, may still use the toolset, and no other
person is signed in on the callback request. Changing the toolset's OAuth settings or deleting it forgets every
person's sign-in; `forget_user_sign_ins(storage_provider, user_id)` removes one person's on every toolset (Phase 6
calls it when a user is removed). The per-user OAuth migration hands each existing browser sign-in to the user who
connected it (`connected_by`), else the first admin; never the toolset's owner, who may never have held that upstream
account. Details: [mcp-oauth](../subsystems/mcp-oauth.md).

### The MCP server catalogue

What an MCP client may list and call is the exposure allowlist AND `is_exposable`, which refuses every tool whose
toolset is not a reserved built-in (`not_system_toolset`; stored toolset rows cannot take a built-in id). So no owned
toolset, and no person's MCP OAuth sign-in, is ever reachable over `/v1/mcp`, whatever the allowlist says; this is
pinned by `tests/mcp/test_safety.py`. The exposed built-in tools run as the MCP caller's person (`current_actor`, and
the acting person the middleware set) and check access like any other door.

### Event attribution

Every CRUD and action event names the person behind it. Both storages' CRUD writers (`_append_crud_event`) and
`EventRecorder.emit(actor=None)` record `actor_label(acting_person())`: `user:<user id>` for a person (a bearer call is
its token's owner), `system` for the system person or when nobody acts. An explicit `actor=` still wins, and
`actor_of(ref)` renders `user:<user_id>` whenever the ref carries a user id, so `mcp.tool_called` names the user. The
acting agent is not part of the label (approvals carry the full identity). The console's Activity view shows a
`user:<id>` actor as the username.

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

### The person-filtered tool list

A run's tool list holds only the tools its person may use. `toolset_use(sp, person, toolset_ids)`
(`primer/access/tools.py`) answers, for each toolset id as spelled, `None` (every tool), a set (only those tools) or an
empty set (none), with ONE batched read of the toolset rows. A toolset with no row (a built-in or reserved id) is open to
every full user; a row the person holds a level on gives every tool; otherwise only the tools the row marks public
(`public_tools`, `may_use_tool`), exactly those and never their siblings; a restricted person gets nothing; admins
short-circuit with no read.

- `ToolExecutionManager.list_tools` computes it for `person_from_ref(initiated_by)` through the provider registry's
  storage provider, BEFORE any remote (MCP) toolset is listed. A toolset with no usable tool is left out and reported with
  the notice word `not available to you` (`tool_listing.NO_ACCESS`); it is never listed remotely, so no connection and no
  OAuth prompt happen for it. A toolset with some usable tools is reported `some tools not available to you`
  (`SOME_TOOLS`) and its other tools stay out of the routing table. Both words join `NOTICE_STATES`, the system-message
  notice a model already reads for an unreachable toolset. A manager built with no registry skips the check (tests only).
- Every inner door asks the same question again: `dispatch_inner_tool(storage_provider=...)` runs `inner_access_refusal`
  beside the role floor and answers a refused tool as `not-found` (the words of a missing tool), so an approved
  `call_tool` re-run after a revoke is refused. `system__call_tool`, `list_toolset_tools` and `describe_tool` read the row
  with `_toolset_read_refusal`, which lets a public tool through; `list_toolset_tools` lists only the usable tools and
  `describe_tool` answers any other as missing.
- A graph ToolCall manager with no identity is an unattributed user, never the system principal.
- The REST catalogues (`/v1/tools`, `/v1/tools/catalogue`, the toolset catalogue in `providers.py`) use
  `catalogue_predicate`: the rows the person can see plus the rows carrying the derived member token `tools:public`
  (`TOOLS_PUBLIC_TOKEN`), and show only the public tools of a row the person holds no level on. `visible_predicate` never
  matches that token, so the row itself stays unlisted and unreadable.

### Grantable infrastructure

Model profiles and the providers a user-owned entity references carry a use grant: `Grantable`
(`primer/model/access.py`) gives them an `access` block with no owners, every grant `read` (meaning *use*) and `public`.
The seven kinds are model profiles, LLM providers, embedding providers, cross-encoder providers, vector store (semantic
search) providers, workspace providers and channel providers, each with a GIN index on `access.members`. Web search and
fetch, speech and artifact storage providers stay global settings (below), not grantable.

- Writes stay admin-only: creating, editing and deleting a row, and setting its grant. `make_crud_router(grantable=True)`
  stamps a private block (`Access()`) on create and ignores the body's `access`; an update keeps the stored one through
  `update_keeping_access`, so no edit, probe stamp or bootstrap re-run resets a grant. The tool twin is
  `_crud_tools_for(grantable=True)`.
- The sub-resource `GET` / `PUT /v1/{plural}/{id}/access` is mounted on the USER tier before the admin CRUD routers
  (`primer/api/routers/infra_access.py`), and the service decides: `set_access(use_only=True)` refuses owners, `full`
  grants and per-tool flags and skips the harness-managed refusal, and only an admin holds `owner`, so only an admin can
  change it. A grantee (or any reader of a public row) reads the view, but a non-admin's view lists no owners and no
  grants, only `your_level` and `public` (`get_access_view(use_only=True)`), so a colleague's grant cannot be enumerated.
  A stranger gets 404. `system__get_entity_access` / `set_entity_access` take the seven kinds (`_USE_ONLY` in
  `_system_access.py`) and call the same service.
- `usable(sp, person, model_cls, id)` (`primer/access/infra.py`) is the one question every check asks: the row, or
  `None` when it is missing or the person holds no level on it, so an unusable row reads exactly like a missing one.
- New rows are private; rows the platform writes are public: the bootstrap runner's providers, and the profile the
  public seeded agents run on, which `publish_default_profile` (`primer/bootstrap/seed.py`, called by the ensure pass on
  boot and `/setup/seed`) makes public together with its members and their LLM providers, only while no admin ever shared
  them (`rev` 0). Existing rows become public in migration 26.
- The choices lists (`GET /v1/model_profiles/choices`, `system__list_model_profile_choices`,
  `GET /v1/workspace_providers/_choices`, `list_workspace_provider_choices`) take the person's `visible_predicate`.

### Use checks on profiles and providers

The saver is checked when an entity that names infrastructure is written, and the person a run acts as is checked when it
is used. An unchanged reference is not re-checked on an edit, so an edit by a full grantee checks the grantee only on
what the edit adds.

- **Model profiles.** `resolve_llm(person=)` (`primer/model_profile/resolver.py`) is a USE: `require_profile_use` needs
  use of the profile the run names (the session override, else the agent's, or a graph node's) AND of every LLM provider
  it resolves to, the providers of an aggregated profile's members included. The member recursion carries no person: a
  grant on an aggregate covers its members, a grant on a member never covers the aggregate. The agent, graph and
  sub-agent builders pass `person_from_ref(initiated_by)` (a sub-agent: the parent run's identity), manual compact the
  requester. A refusal raises `ProfileUnusable` (`primer/authz.py`, code `model_profile_unusable`); it fails the TURN,
  never the session: `pause_session_for_unusable_profile` writes an ERROR record and `last_turn_error.code`, sets
  PAUSED, keeps the park and releases the lease with `entity_noop`, and the pool's resume branch does the same, so an admin
  grant then `/resume` runs it and a peer without the grant who steers cannot end the owner's session. `invoke_agent`
  answers a tool error (`forbidden`). On save, `check_profile_exists` and the new `check_graph_node_profiles`
  (`primer/agent/agent_checks.py`) refuse a profile the saver may not use as `model_profile_not_found`, and a toolset row
  the saver cannot read (none of whose named tools is public) counts as missing; REST and the tools both pass the saver.
  `GET /v1/agents/{id}/status` reports an unusable profile as ONE issue and skips the provider and member walk.
- **Workspace providers.** `WorkspaceRegistry.materialise(person=)`, the one door every create passes, needs use of the
  template's provider and answers an unusable one with the not-found `get_backend` raises for a missing one, building
  nothing; the bootstrap seed passes no person. A template save checks its saver (`primer/workspace/provider_use.py`;
  422 `workspace_provider_not_found`, a provider that does not exist reads the same).
- **Channel providers.** A channel save needs use of its provider (`check_channel_on_create` / `check_channel_on_update`
  with `person`), and binding a channel (`channel_for_binding`: a workspace's reply binding, workspace create, the
  `set_reply_binding` tool, a channel trigger) needs it too; otherwise the channel reads as missing.
- **Search providers.** `enable_search(person=)` needs use of the embedder, the vector store and the cross-encoder
  (else the existing not-registered 409), and so does every search (`primer/knowledge/search_use.py`): REST answers 409 and
  points to grep and the tree, the `collections` tool falls back to fulltext on `auto` and refuses `semantic`. Indexing
  writes by full users are not a use of their own.

### Global settings

Install-wide settings every feature already uses are readable by every full user, secrets masked, and writable only by
admins: the web search and fetch providers and their active configs, the speech providers and active config, the artifact
storage providers and active store, canvas settings and the MCP exposure singleton. The routers split by verb with
`read_write_routers` (`primer/api/routers/_crud.py`): GETs mount at the user tier (a restricted user still reaches
nothing, an unauthenticated caller gets 401), the writes, the `_test` probes and the active-config PUTs stay admin.

### Console (Phase 4)

The share dialog (`ui/components/shared/share-dialog.jsx`) has a `useOnly` mode for the seven grantable kinds: no owners
and no level picker, only "People who can use" and an "Everyone can use" switch; admins edit it, everyone else sees it
read-only (their own level and public). A "Who can use" button (the `entity.share` verb) sits on the detail overlays of
model profiles, LLM, workspace, channel, semantic-search, embedding and cross-encoder providers, and their cards carry a
`SHR_useBadge` (Public, "N people" or "Admins only"). The create forms of these kinds have a "Public" switch, off by
default (a new row is private). The agent form and the graph node profile picker show a current profile the person cannot
use as "<id> (not available to you)" with a warning, and a paused session whose `last_turn_error.code` is
`model_profile_unusable` shows a banner saying its model profile is not available to the person who sent the turn and that
an admin can grant its use before Resume. Tests: `tests/ui/test_p4_use_grants_ui.py`.

The subsections below describe Phase 3 (collections, triggers, channel triggers and harnesses). They are numbered
from 6.8 so they follow the Phase 2 subsections (workspaces and what inherits from them, the turn person, events, the
per-user default workspace) of the same section.

### 6.8 Collections and what documents inherit

A collection is owned; a document has no access of its own and is checked against its collection on every door (by
check, not by copy). The REST helper is `_require_collection` (`primer/api/routers/knowledge.py`), the tool helper
`collection_for` (`primer/toolset/_collection_access.py`); both answer a collection the person cannot read exactly like
a missing one (REST 404 with the missing-row text, tool `type=not-found`), too low a level with REST 403
`access_denied` / tool `type=forbidden`, and refuse any write to a `system` collection whoever asks.

| Level | REST | Tools |
|---|---|---|
| read | `GET /collections/{id}`, the list and find (filtered in storage), `/access`, `/docs` (read and tree), `/grep`, `/documents` (list and read), `POST /search`, `GET /search` (status), `/indexed_documents`; the flat `/v1/documents` list and find (`collection_id IN` the readable collections, from `readable_ids`) and get | `collections_list` (filtered), `collection_tree`, `read_document`, `search`; system `list/get/find_collections`, `list_collection_documents`, `find_collection_documents_by_meta`, `search_collection`, `get_document_content`, `list_documents`, the generic `get_document` and `find_documents` (same filter), `get_entity_access`; `wiki_read` and `wiki_search` act as the caller on `system` |
| full | `PUT /collections/{id}` (keeps the stored access and the stored search block), `POST/PATCH/DELETE /docs`, `/docs/move`, `PUT/DELETE /documents`, `/documents/move`, `POST /import`; the flat `/v1/documents` create (full on its collection), update and delete (a move into another collection needs full there too) | `create/update/move/delete_document` (collections toolset), system `update_collection`, `put_document`, `move_document`, `create/update/delete_document` |
| owner | `DELETE /collections/{id}`, `PUT /{id}/access`, `PUT` and `DELETE /{id}/search` | system `delete_collection`, `set_entity_access`, `refresh_collection` (re-indexing is search management) |

The search block is owner-only: a whole-row update (`PUT /collections/{id}`, `update_collection`) whose search
configuration differs from the stored one is refused for everyone, owner included (403
`collection_search_protected`, tool `type=forbidden`), and points to `PUT` / `DELETE /collections/{id}/search`, which
validate the providers and backfill or drop the index. A body that sends the stored configuration back passes, and the
stored block (with its lifecycle `state` and `error`) is what is written (`check_collection_search_unchanged` in
`primer/knowledge/checks.py`, called from both update hooks).

### 6.9 Special collections

- `system` (the platform wiki) is created `Access(public=True)` and every regeneration turns `public` back on (a
  rev-guarded patch that keeps any other sharing), so every full user reads it and the wiki tools work for everyone.
  Its `system` flag keeps it read-only: every write is refused, an admin's included.
- `_internal_*` collections carry `Access()`: no owners, not public, so only admins reach them.
- The `this-install/` live map lists only public entities: a private agent, graph, collection or workspace has no page
  and no index entry, and a page whose entity goes private is dropped (by `converge_entity` and by the boot prune).
  Provider pages are still listed: providers have no `access` yet.

### 6.10 Triggers and subscriptions

A trigger is owned; a subscription has no access of its own and is checked against its trigger. Every function of
`primer/trigger/service.py` that takes `person` enforces the level through `_require_trigger`; `person=None` is an
internal (platform) call that is not gated, so every REST route and tool must pass the caller:
`tests/trigger/test_service_calls_name_the_person.py` parses `primer/api` and `primer/toolset` and fails on a call
that omits `person=`.

| Level | What it allows |
|---|---|
| read | get, list (filtered in storage), `/access`, the subscription list and get, `list_channel_bindings`, parking on the trigger (`subscribe_to_trigger`, `subscribe_to_channel_event`) |
| full | `PUT` (update), `fire_now`, subscription create, update and delete, `create_channel_binding`, `delete_channel_binding` |
| owner | `DELETE`, `rotate_token`, `PUT /access` |

No level answers `trigger_not_found` (REST 404, as an unknown id); too low a level REST 403 `access_denied`, tool
`type=forbidden`. These levels replace the rank rules of tickets 01a16e65-12d1 and 01a16ee4 (`refuse_fire_now`,
`refuse_trigger_change`) and the webhook manage rule on update and rotate.

Who a fire runs as is unchanged: the trigger's `owner` (its last saver) and each subscription's `owner`, ranked by
the lower of their current roles (`principal_for_fire`, `primer/trigger/owner.py`). A full grantee who saves becomes the
run-as owner and acts as themselves; a save that changes the run-as owner of a webhook trigger mints a new token.
`view_for` still masks the webhook token and reduces the owner to `display` and `role` for anyone but the run-as owner
or an admin. A subscription with no owner is refused at fire time (`refuse_fire_out_of_reach`, error code
`target_out_of_reach`; fail closed: the migration gave every legacy row an owner and disabled it until that owner
re-enables it), and an admin or system owner passes that check without a workspace lookup.

### 6.11 Channel triggers

A channel trigger's events are channel content, so whoever saves one, or binds a subscription onto one, needs read on
the channel it fires from (`require_channel_trigger_access`): the REST create and update, the trigger tools and
`create_channel_binding`. A channel the person cannot read answers like a missing one; a provider-wide trigger
(`channel_id` null, listening to every channel of the provider) is admin-only (403 `access_denied`). The check at save
time is the gate: `primer/channel/event_dispatch.py` does not check access when it dispatches an event.

### 6.12 Harnesses

A harness is owned. What it installs (agents, graphs, toolsets, collections; documents follow their collection) carries
a COPY of the harness's access: an install or sync creates a row with `access = harness.access`, and a sync updates the
other fields with `rewrite_keeping_access`, never the access. Sharing the harness (`PUT /v1/harnesses/{id}/access`,
`set_entity_access` kind `harness`) runs `apply_harness_access` after the write: one field-scoped patch of `access` per
installed row, ordered by `access.rev` (a row already at a higher rev is left alone, and each write is fenced on the rev
it read, so two racing fan-outs cannot resurrect a revoked grant) and only while the row still belongs to the harness.
Sharing an installed row directly answers 409 `harness_managed`. Harness writes (create, update, install, sync, build,
push, uninstall) stay admin-only in this phase; list, get and the bundle need read, and list filters in storage. The
outbound export strips `access` from the templates it writes.

### 6.13 Whole-row writers

Every writer that changes some fields of an owned row writes those fields only, or writes every field but `access`, so
an owner's sharing change made in between is never undone:

| Writer | How |
|---|---|
| Owned CRUD `PUT` and the generated update tools; trigger update and rotate | `update_keeping_access` (409 on a sharing change in between) |
| Harness syncs, the harness routes and tools that rewrite the row | `rewrite_keeping_access` (fenced on a fresh rev, retried) |
| The search lifecycle (`_write_search`), internal-collection materialisation, the system wiki's title | `write_collection_fields` |
| The system wiki's `public` | a rev-guarded `patch_if` of `access` |
| A fire's bookkeeping (`last_fired_*`), the trigger claim release (`enabled`, `next_fire_at`) | `write_trigger_fields` |

### 6.14 Phase 3 migrations

- `m023_collection_ownership`: an unset collection is owned by the first enabled admin, `system` becomes public,
  `_internal_*` stays unset (admin-only).
- `m024_trigger_ownership`: an unset trigger is owned by the user behind its run-as owner (a token resolves to its user),
  else the first enabled admin. An ownerless subscription gets its trigger's run-as owner (the first admin when the
  trigger has none) so that owner can see it, and is DISABLED: nobody recorded who saved it, and any adopted identity
  would give a possibly planted row the owner's role and reach (their private workspaces and sessions). The trigger's
  owner re-enables it with an ordinary update, which needs full and makes the re-enabler its owner.
- `m025_harness_ownership`: an unset harness is owned by the first enabled admin; every installed row takes a copy of
  its harness's access.

## 7. Testing patterns

- `tests/access/`: `Person` derivations and the policy, including a matrix that pins list visibility to `level_of` for
  every row and person (`test_list_and_get_agree_on_every_row_and_person`).
- `tests/api/test_crud_owned.py` and `tests/api/test_access_routes.py`: a minimal app with a test-only middleware that
  turns an `X-Test-User: <id>:<role>` header into `request.state.actor`, on a real SQLite provider.
- `tests/toolset/test_system_access_tools.py`: tool refusals and the MCP path (set and reset `current_actor`).
- `tests/storage/test_access_index.py`: the GIN index and its use by the list query (Postgres lane).
- `tests/storage/test_m021_entity_ownership.py`: the migration rules and the public seeded rows.
- Phase 3: `tests/api/test_collection_access.py` (every collection and document door by level),
  `tests/toolset/test_collection_tools_access.py`, `tests/knowledge/test_collection_access_special.py` (special
  collections, the live map, revokes racing the platform writers), `tests/trigger/test_trigger_access.py`,
  `tests/toolset/test_trigger_tools_access.py` (including the channel-binding tools),
  `tests/trigger/test_service_calls_name_the_person.py` (the static guard), `tests/trigger/test_channel_trigger_access.py`,
  `tests/harness/test_harness_access.py`, and `tests/storage/test_m023_collection_ownership.py`,
  `test_m024_trigger_ownership.py`, `test_m025_harness_ownership.py`. The two-user journey is
  `tests/e2e/test_access_p3_journey.py`.
- Phase 4: `tests/access/test_tool_use.py` and `tests/agent/test_tool_list_access.py` (the filtered list),
  `tests/access/test_use_grants.py`, `tests/api/test_infra_use_routes.py` and `tests/toolset/test_system_access_infra_kinds.py`
  (grantable infrastructure), `tests/model_profile/test_profile_use.py` and `tests/session/test_unusable_profile_turn.py`
  (profile use), `tests/workspace/test_materialise_access.py`, `tests/channel/test_channel_provider_use.py`,
  `tests/knowledge/test_search_provider_use.py`, `tests/api/test_global_settings_read.py`, and the live journey
  `tests/e2e/test_access_p4_journey.py` (CI only).
- `tests/access/test_acting_person.py` and `tests/events/test_event_attribution.py`: the acting person (middleware,
  bearer owner, auth disabled, reset after the request, `person_for_tool` order) and the event actor (REST, bearer, a
  run's tool write, a run overriding an inherited request person, MCP, worker internals, an explicit actor); the
  Postgres twin is `tests/storage/test_crud_event_actor_live.py`.
- `tests/toolset/oauth/test_per_user_tokens.py`, `tests/api/test_mcp_oauth_per_user_routes.py` and
  `tests/storage/test_m027_mcp_oauth_per_user.py`: MCP OAuth per person, its routes and its migration;
  `tests/e2e/test_access_p5_journey.py` drives the whole flow against the mock OAuth MCP server.

## 8. Historical decisions

- Access lives on the row rather than in a side table: one write changes it atomically, the CRUD event carries it, and
  the list filter is a single indexed predicate with no join.
- `public_tools` alone does not make a toolset listable: listing and reading must agree, and single public tools are
  reached through the tool catalogue, not the toolset row.
- The sessions-are-a-shared-read decision (G7-02) is reversed by this model; sessions follow their workspace once
  workspaces are owned.
- Inheritance is by check where the parent is always at hand (documents, subscriptions) and by copy where the child is
  an independent owned row that other code lists and reads on its own (harness contents): a copy keeps every list a
  single indexed predicate. Either way a child is never wider than its parent.
- The trigger rank rules were replaced by the access levels: a rank says who may drive whose automation, a level says
  who was given what, and the owner of a trigger now decides who may fire or edit it.
