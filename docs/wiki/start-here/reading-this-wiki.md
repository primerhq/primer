---
title: Reading this wiki
summary: "The tree of the wiki, and how wiki_read and wiki_search navigate it."
tools: [system__wiki_read, system__wiki_search]
related: [start-here/calling-tools]
---
## What it is
The platform wiki: every feature an agent can use, documented as pages with the exact tool calls. It has one table of contents, section folders, a generated tool reference, and a live map of this install.

## Concepts
The tree of the wiki:

- [index](index) - the table of contents. `system__wiki_read` with an empty path reads it.
- sections - every folder holds its own page in its `index.md`; [start-here](start-here) is this one. P4 adds the feature sections.
- [reference/tools](reference/tools) - one generated page per tool, rendered from the tool's own descriptor: the validating call, the argument schema, the role, the gate. It cannot drift from the code.
- [this-install](this-install) - the live map of this install: the agents, graphs, collections, workspaces and providers that exist here, written at boot and on every change.
- the legacy part - the shipped agent docs under the `docs/` prefix and the `how-to/` guides, read alongside until the feature pages replace them.

Links in the wiki are wiki paths, never URLs: no leading slash, no `.md`, an optional `#anchor`. A link that goes nowhere is a defect, not a style.

## How to
1. Read [start-here](start-here) once; the other start-here pages are the contract for calling tools.
2. For a feature, open its section page; for a tool, open its reference page before you call it.
3. When you only have words, search first: `system__wiki_search` finds the pages, then `system__wiki_read` reads one.
4. Read a tool's reference page or call `system__describe_tool` before you call the tool: the reference page is the tool's own schema, and `system__describe_tool` is the live answer.

## Tool calls
These are core-kit tools, called directly by their scoped id (not through call_tool):

```json
{"name": "system__wiki_read", "arguments": {"path": "start-here/reading-this-wiki"}}
```

```json
{"name": "system__wiki_search", "arguments": {"query": "approval", "mode": "fulltext"}}
```

## Gotchas
- A path is a slug path: `start-here/reading-this-wiki`, not the file's `.md` and not a URL.
- `system__wiki_search` defaults to `mode: auto`; on a collection whose semantic indexing failed it falls back to fulltext and says so in the result. It does not invent hits.
- The generated reference pages are rebuilt from the code at boot: a page that disagrees with the live schema is a stale install, not a stale page.

## Related
[calling tools](start-here/calling-tools), [the core kit](start-here/the-core-kit).
