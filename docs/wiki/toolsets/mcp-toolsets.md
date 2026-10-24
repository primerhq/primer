---
title: "MCP toolsets"
summary: "Register an MCP server as a toolset, change it, and read its sign-in status."
order: 2
tools: [system__list_toolsets, system__get_toolset, system__create_toolset, system__update_toolset, system__delete_toolset, system__find_toolsets, system__get_toolset_auth_status]
related: [toolsets/list-and-describe-tools, approvals/policies]
---
## What it is
An MCP toolset is a stored `Toolset` row with `provider: "mcp"` that makes the tools of one MCP server callable in primer. You create, change and delete the row with the `system` toolset's tools, and read its sign-in state with `system__get_toolset_auth_status`.

## Concepts
- `config.transport` picks how primer talks to the server, and `config.config` must match it:
  - `http` (streamable HTTP) or `sse` (the older HTTP and SSE protocol): a `url`, optional `headers` and an optional `oauth` block.
  - `stdio`: a `command` (the argv list) and an optional `env`. Primer starts the command on the server host, as a fresh process for every listing and every call.
- Three ways to authenticate. No auth. A header, such as `Authorization` or `X-Api-Key`. Or an `oauth` block: `grant` is `authorization_code` (a person approves access in a browser, the default) or `client_credentials` (the server trades `static_client`'s id and secret for a token, nobody present). The block also takes `scopes`, `resource_uri` (the server's URL or a parent of it on the same origin), `static_client` (skips dynamic client registration) and `token_endpoint` (client credentials only).
- One sign-in per toolset, shared by every agent that uses it. Only an admin can connect it, from the console. You cannot start a sign-in: read the state and tell the user.
- Header values, `env` values and the OAuth client secret are secrets. Every read serves them masked.
- The tools are named `<toolset id>__<tool>` (`docs-mcp__search_docs`). Grant them in an agent's `tools` list, or call them through `system__call_tool` with the toolset id as `toolset_id`. Every MCP tool runs at the `user` floor; to gate one, add an [approval policy](approvals/policies). A server tool that runs as an MCP task parks the turn until the task ends.
- Who may write what:
  - Any user may create or change an `http` or `sse` toolset.
  - Creating or changing a `stdio` toolset needs an admin, either way round: turning an http toolset into stdio, or editing a stdio one. A Python body needs an admin too.
  - Changing the `url` or the `oauth` block of a toolset that signs in with OAuth needs an admin, and the change signs it out.
  - Changing the URL while you send a masked secret back needs an admin.
- The approval gate. A fresh install gates `system__create_toolset` and `system__update_toolset` with a rule on the body: an `http`, `sse` or `internal` body runs at once, anything else (`stdio`, `python`) parks for an admin's approval, whatever your rank.
- The status: `mode` is `none`, `header`, `oauth_browser` or `oauth_client_credentials`; `state` is `connected`, `needs_sign_in`, `error` or `not_applicable`, with a `reason` and an `expires_at`.

## How to
1. Create the row with `system__create_toolset`.
2. Check that the server answers: list its tools with `system__list_toolset_tools` (see [list and describe tools](toolsets/list-and-describe-tools)).
3. For an OAuth toolset, read `system__get_toolset_auth_status`. On `needs_sign_in`, ask the user to have an admin open the toolset in the console and connect it, and wait.
4. Grant the tools to an agent, or call them through `system__call_tool`.

## Tool calls
### Create an HTTP toolset
`system__create_toolset` stores the row from the full `entity`; an http body runs ungated.

```json
{"toolset_id": "system", "tool_name": "create_toolset", "arguments": {"entity": {"id": "docs-mcp", "provider": "mcp", "config": {"transport": "http", "config": {"url": "https://docs.example.com/mcp", "headers": {"X-Api-Key": "REPLACE"}}}}}}
```

Reference: [`system__create_toolset`](reference/tools/system/create_toolset)

### Get a toolset
`system__get_toolset` reads one stored row, secrets masked.

```json
{"toolset_id": "system", "tool_name": "get_toolset", "arguments": {"id": "docs-mcp"}}
```

Reference: [`system__get_toolset`](reference/tools/system/get_toolset)

### Turn on OAuth sign-in
`system__update_toolset` replaces the whole row: send the id and the full entity. Here the header goes and an `oauth` block arrives, so an admin must connect the toolset before its tools answer.

```json
{"toolset_id": "system", "tool_name": "update_toolset", "arguments": {"id": "docs-mcp", "entity": {"id": "docs-mcp", "provider": "mcp", "config": {"transport": "http", "config": {"url": "https://docs.example.com/mcp", "oauth": {"grant": "authorization_code", "scopes": ["docs.read"]}}}}}}
```

Reference: [`system__update_toolset`](reference/tools/system/update_toolset)

### Read the sign-in status
`system__get_toolset_auth_status` answers the shared sign-in's mode, state, reason and expiry, never a token.

```json
{"toolset_id": "system", "tool_name": "get_toolset_auth_status", "arguments": {"toolset_id": "docs-mcp"}}
```

Reference: [`system__get_toolset_auth_status`](reference/tools/system/get_toolset_auth_status)

### List toolsets
`system__list_toolsets` pages through the stored toolsets.

```json
{"toolset_id": "system", "tool_name": "list_toolsets", "arguments": {"limit": 20}}
```

Reference: [`system__list_toolsets`](reference/tools/system/list_toolsets)

### Find toolsets
`system__find_toolsets` filters the stored rows by field values.

```json
{"toolset_id": "system", "tool_name": "find_toolsets", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "provider"}, "op": "=", "right": {"kind": "value", "value": "mcp"}}}}
```

Reference: [`system__find_toolsets`](reference/tools/system/find_toolsets)

### Delete a toolset
`system__delete_toolset` removes the row and forgets its shared sign-in first.

```json
{"toolset_id": "system", "tool_name": "delete_toolset", "arguments": {"id": "docs-mcp"}}
```

Reference: [`system__delete_toolset`](reference/tools/system/delete_toolset)

## Gotchas
- The tool does not probe the server, so a create with a wrong URL succeeds. Check with `system__list_toolset_tools` before you grant the tools.
- An update replaces the whole row. Read it, change it, send it all.
- A masked secret sent back unchanged keeps the stored value only for the same origin (scheme, host and port). Move the URL to another origin and the update answers `validation-error` ("re-enter the secret"): send the real value. A create that carries a mask, such as a row copied under a new id, is refused the same way.
- A user-role run's `stdio` write parks first and is then refused `forbidden` once approved. Nothing is stored either way: ask an admin.
- `system__delete_toolset` answers `conflict` while an approval policy names the toolset. Deleting an OAuth toolset forgets its sign-in, so one created again under the same id needs an admin to connect it.
- A toolset a harness manages (`harness_id` set) cannot be changed or deleted here (`conflict`): change it through its harness.
- An operator can limit which commands a `stdio` toolset may start (`mcp_stdio_allowed_commands`). A command outside the list fails when the toolset lists or runs.
- At the start of a turn, a server that is down, needs a sign-in or does not list within 60 seconds is left out of that turn, and the system message says why. A cold `stdio` start counts against those 60 seconds. See [tools and grants](agents/tools-and-grants).
- The shared sign-in means every agent acts as whoever connected it. Use separate toolsets for separate identities.
- The tools of a stored toolset are never offered to outside MCP clients through primer's own endpoint. That direction is on [not agent-callable yet](not-agent-callable).

## Related
[List and describe tools](toolsets/list-and-describe-tools), [approval policies](approvals/policies), [built-in toolsets](toolsets/builtin-toolsets), [Python toolsets](toolsets/python-toolsets).
