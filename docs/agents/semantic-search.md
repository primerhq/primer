---
slug: semantic-search
title: Semantic search - the system collection and internal search
summary: How an agent finds this install's agents, graphs, collections, tools and these docs - search and read the system collection, by keyword always and by meaning once internal search is on.
related: [knowledge, agents, graphs, mcp-exposure]
mcp_tools:
  - system::wiki_search
  - system::wiki_read
  - system::describe_tool
  - collections::search
  - collections::read_document
  - collections::collection_tree
---

# Semantic search - the system collection and internal search

## Overview

Primer has two search surfaces that agents often confuse. The first is
user-defined knowledge: a `Collection` an operator created, with
documents written into it by path ([knowledge](knowledge.md) covers
it). The second is how an agent discovers what this install holds:
the reserved `system` collection, a read-only wiki of the platform that
primer writes itself. This doc is about the second.

There is no `search` toolset and no per-entity search tool. Discovery
is reading and searching the `system` collection, with either pair of
tools:

- `system::wiki_search` and `system::wiki_read`, the two wiki tools of
  the core kit that every seeded agent holds;
- `collections::search` and `collections::read_document` (plus
  `collections::collection_tree`) with `"collection": "system"`.

Search is by keyword on every install. Once an admin turns on internal
search (System › Internal collections in the console), the `system`
collection also gets a semantic index, and the same calls in
`mode: "auto"` search by meaning.

Use these tools when you have a natural-language question ("is there
an agent that reviews pull requests?", "which tool fetches a web
page?", "how do triggers catch up?"). When you already know an id, read
it directly: `system::get_agent`, `system::get_graph`,
`system::describe_tool`.

## Mental model

The `system` collection holds, by path:

- `index` - the table of contents (`system::wiki_read` with an empty
  path reads it).
- The platform wiki's hand-written guides, for example
  `start-here/calling-tools`.
- `reference/tools/<toolset>/<tool>` - one generated page per
  agent-callable tool, with its arguments and an example call.
- `docs/<slug>` - these agent docs (`docs/agents`,
  `docs/semantic-search`, `docs/cookbook/discover-a-capability`), kept
  as the wiki's legacy layer until the wiki replaces them.
- `this-install/` - the live map of this install:
  `this-install/agents/<id>` (each agent's description and model
  profile), `this-install/graphs/<id>` (each graph's description),
  `this-install/collections/<id>` (each collection's description and
  its best search rung), `this-install/workspaces` and
  `this-install/providers`.

The live map is rebuilt from live state at every startup, and an
agent, graph or collection page is rewritten when that row changes. An
id made only of lowercase letters, digits, `.`, `_` and `-` is its own
slug; any other id gets a cleaned slug, so read the path a search hit
or the section page gives you rather than building it.

A search answers `{hits: [{path, excerpt, score}], truncated,
mode_used}` (a `literal` hit carries `line` instead of `score`). Read a
hit's page with `system::wiki_read` (`{path}`) or
`collections::read_document` (`{collection: "system", path}`): unlike
the old chunk-only search, the whole page is readable.

`system::wiki_search` searches the whole collection except
`this-install/`, which it searches only when `path_prefix` is
`this-install` or a path under it. `collections::search` on `system`
has no such exclusion.

## Lifecycle and states

Internal search has three observable states; the `system` collection
is readable in all of them:

- **off** - no `InternalCollectionsConfig` row exists (a fresh
  install). The `system` collection is there and readable, and search
  is by keyword: `mode: "auto"` answers `mode_used: "fulltext"`, and
  `mode: "semantic"` is refused with `type=conflict` ("semantic search
  is not enabled on 'system'").
- **indexing or error** - an admin configured it (embedding provider
  and model, vector store, optional cross-encoder) and enabled it, and
  the index is still building or failed to build. `mode: "auto"` falls
  back to keyword search and the result carries a `note` that says so.
- **on** - the semantic index is ready. `mode: "auto"` answers
  `mode_used: "semantic"`.

An admin turns it on with `PUT /v1/internal_collections/config` and
then `POST /v1/internal_collections/bootstrap`, which enables semantic
search on the `system` collection and indexes it, and off with
`DELETE /v1/internal_collections/config`, which disables it and drops
the collection's vectors (keyword search keeps working).
`GET /v1/internal_collections/bootstrap/status` reports the index
state. These routes are admin-only REST, not agent tools.

## MCP tools

### `system::wiki_search`

**Purpose.** Search the `system` collection. Arguments: `query`
(required), `mode` (`auto`, the default, `semantic`, `fulltext` or
`literal`), `path_prefix` (for example `this-install/agents/` or
`reference/tools/`) and `limit` (1 to 50, default 10).

**Returns.** `{hits: [{path, excerpt, score}], truncated, mode_used}`.

### `system::wiki_read`

**Purpose.** Read one page by path; an empty path reads the table of
contents. Leading or trailing slashes, a trailing `.md` and a `#anchor`
are stripped.

**Returns.** `{path, title, summary, kind, body, children}`. A missing
path is `is_error=true` with `type=not-found`.

### `collections::search`, `collections::read_document`, `collections::collection_tree`

**Purpose.** The general collection tools, pointed at
`"collection": "system"`. `collections::search` takes the same
arguments as `system::wiki_search` plus `mode: "regex"` (`limit` 1 to
100, default 20) and does not skip `this-install/`.
`collections::collection_tree` lists a subtree, for example
`{"collection": "system", "parent": "this-install/agents", "depth": 1}`
for every agent on the install.

### `system::describe_tool`

**Purpose.** Once a search has named a tool, read its exact arguments
and the call to make: `{"toolset_id": "web", "tool_name":
"http_request"}`.

## Workflows

### Workflow 1 - find the right tool for a job

**Goal.** Agent needs to fetch a web page; it knows roughly what it
wants but not the tool's name.

1. Search the tool reference:

```json
{
  "tool": "system::wiki_search",
  "arguments": {"query": "fetch a web page over HTTP", "path_prefix": "reference/tools/"}
}
```

Response:

```json
{
  "hits": [{"path": "reference/tools/web/http_request", "excerpt": "...", "score": 0.82}],
  "truncated": false,
  "mode_used": "semantic"
}
```

2. Read the exact arguments and the call to make:

```json
{
  "tool": "system::describe_tool",
  "arguments": {"toolset_id": "web", "tool_name": "http_request"}
}
```

3. Call the tool the way the description says (through
   `system::call_tool` for a seeded agent).

### Workflow 2 - find an agent to run

1. Search the live map's agents:

```json
{
  "tool": "system::wiki_search",
  "arguments": {"query": "review a pull request", "path_prefix": "this-install/agents/"}
}
```

2. The top hit is `this-install/agents/code-reviewer`. Read the full
   agent with `system::get_agent`:

```json
{
  "tool": "system::get_agent",
  "arguments": {"id": "code-reviewer"}
}
```

### Workflow 3 - learn how a feature works before using it

1. Search the docs and guides:

```json
{
  "tool": "system::wiki_search",
  "arguments": {"query": "cron trigger fire scheduled catch-up", "limit": 3}
}
```

2. A hit on `docs/triggers-and-subscriptions` covers the catch-up
   policy. Read the whole page:

```json
{
  "tool": "system::wiki_read",
  "arguments": {"path": "docs/triggers-and-subscriptions"}
}
```

3. Now the agent can call `trigger::create` with the right config.

## Gotchas

- **There is no `search` toolset.** Older notes and prompts may name
  per-entity search tools; none exists. Use `system::wiki_search` or
  `collections::search` on `system`.
- **Keyword until internal search is on.** `mode_used` says which rung
  answered. Use `mode: "auto"`: `mode: "semantic"` is refused while the
  index is off.
- **`system::wiki_search` skips `this-install/` by default.** To find
  an agent, graph or collection of this install, pass a `path_prefix`
  under `this-install/`.
- **The live map holds definitions, not running work.** To drive an
  agent or graph, see `cookbook/create-and-run-a-session`.
- **An agent's page holds its description and model profile, not its
  system prompt.** A clear description is what makes an agent
  findable.
- **Asymmetric retrieval.** The semantic index embeds the query as a
  query and the pages as documents; a long copy-pasted document as the
  query degrades recall.
- **Changing the embedding model.** An admin turns internal search off
  (`DELETE /v1/internal_collections/config`, which drops the vectors)
  and on again with the new model.
- **Over MCP**, these tools are callable only when the operator's
  allowlist names them (`system__wiki_search`, `collections__search`,
  and so on); see [mcp-exposure](mcp-exposure.md).

## Related

- [knowledge](knowledge.md) - user-defined collections and documents,
  the parallel surface this doc is not about.
- [agents](agents.md) - an agent's description is what its live-map
  page carries.
- [graphs](graphs.md) - a graph's description is what its live-map
  page carries.
- [mcp-exposure](mcp-exposure.md) - only tools in the operator's
  allowlist appear in `tools/list`.
