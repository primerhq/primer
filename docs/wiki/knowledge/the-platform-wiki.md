---
title: "The platform wiki"
summary: "This wiki is the read-only system collection: how it is built, how to read and search it, and where tool reference pages live."
order: 5
tools: [system__wiki_read, system__wiki_search]
related: [start-here/reading-this-wiki, knowledge/search, toolsets/list-and-describe-tools]
---
## What it is
This wiki is the read-only `system` collection, titled "Platform wiki": the platform's own documentation, written for the agents of this install. Read it with `system__wiki_read`, find your way with `system__wiki_search`, and start at [reading this wiki](start-here/reading-this-wiki).

## Concepts
- The collection holds three kinds of pages. The hand-written pages (the sections you read now), one generated reference page per agent-callable tool at `reference/tools/<toolset>/<tool>`, and the live map under `this-install/`.
- A tool's reference page is the exact call: `## What it does`, `## Call it`, `## Arguments`, `## Facts`, `## Examples` and `## Used in` (the feature pages that link it). The root page at [tool reference](reference) lists every tool page.
- The live map under `this-install/` is the state of THIS install: the agents, graphs, collections, workspaces and providers, kept current as they change. It is rebuilt from live state at every boot, and the agent, graph and collection pages update on change.
- The wiki is built at boot from the checked-in sources over the live tool catalog, and a build writes it only when its version stamp is the newest: a stored build a newer build never claims is never deleted. You and no other agent can edit it: it is a system collection, its documents refuse writes and the collection refuses deletion.
- The `system` collection does not need an embedder to be useful. It gains vectors only if an operator enables search on it like any other collection (see [collections](knowledge/collections)); until then its semantic rung is off and search falls back to fulltext.
- `system__wiki_read` takes a `path` (an empty path reads the table of contents; spaces, a leading or trailing slash, a trailing `.md` and a `#anchor` are stripped) and returns the page's `path`, `title`, `summary`, `kind`, `body` and its `children` (each a path, title and summary).
- `system__wiki_search` takes a `query`, a `mode` of `auto`, `semantic`, `fulltext` or `literal`, an optional `path_prefix` and a `limit` of 1 to 50, and searches the same collection. It returns the hits (each a `path`, an `excerpt` and a `score`), `mode_used`, and `truncated` when the hits were cut.

## How to
1. Find a feature: `system__wiki_search` with a natural-language `query` and `mode` at `auto`; read the hit's page with `system__wiki_read`.
2. Get a tool's exact call: `system__wiki_read` on its reference page, `reference/tools/<toolset>/<tool>`.
3. List the agents of this install: `system__wiki_read` on `this-install/agents` (or search it with `path_prefix: "this-install"`).

## Tool calls
### Read a page
`system__wiki_read` reads one page by path; an empty path reads the table of contents.

```json
{"toolset_id": "system", "tool_name": "wiki_read", "arguments": {"path": "start-here/calling-tools"}}
```

Reference: [`system__wiki_read`](reference/tools/system/wiki_read)

### Search the wiki
`system__wiki_search` searches the wiki by content; the live map under `this-install/` is excluded unless the `path_prefix` names it.

```json
{"toolset_id": "system", "tool_name": "wiki_search", "arguments": {"query": "call_tool", "mode": "fulltext"}}
```

Reference: [`system__wiki_search`](reference/tools/system/wiki_search)

## Gotchas
- A missing path answers `type=not-found` and names the siblings it did find: fix the path from the miss, or search again.
- Semantic mode needs search on `system`. On a fresh install it is off, so `auto` falls back to fulltext (with a note) and an explicit `semantic` is refused; the fulltext and literal rungs are the reliable ones until an operator turns search on.
- `system__wiki_search` has no `regex` mode; for a pattern use `literal` with a distinctive substring, or read the page.
- The live map is a snapshot that the platform keeps current: read it for what this install holds, do not write to it, and expect the pages under `this-install/` to appear and disappear with the rows.
- The `kind` of a read page says what it is: `toc` (the table of contents), `section` (a section index), `guide` (a hand-written page), `reference` (a generated tool page) or `live` (the `this-install` map). A read returns the page's `children` as paths, titles and summaries.

## Related
[Reading this wiki](start-here/reading-this-wiki), [Search a collection](knowledge/search), [List and describe tools](toolsets/list-and-describe-tools).
