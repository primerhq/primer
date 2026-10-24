---
title: "Reading this wiki"
summary: "How the wiki is laid out, how to read and search it, and how to find the right page for a task."
order: 1
tools: [system__wiki_read, system__wiki_search, system__describe_tool]
related: [start-here/calling-tools, knowledge/the-platform-wiki]
---
## What it is
The platform wiki: every feature an agent can use, documented as pages with the exact tool calls. It has one table of contents, section folders, a generated tool reference, and a live map of this install.

## Concepts
The tree of the wiki:

- [index](index) - the table of contents. `system__wiki_read` with an empty path reads it.
- sections - every folder holds its own page in its `index.md`; [start-here](start-here) is this one, and the table below maps each section to the task it covers.
- [reference/tools](reference/tools) - one generated page per tool, rendered from the tool's own descriptor: the validating call, the argument schema, the role, the gate. It cannot drift from the code.
- [this-install](this-install) - the live map of this install: the agents, graphs, collections, workspaces and providers that exist here, written at boot (and updated on every agent, graph or collection change).
- the legacy part - the shipped agent docs under the `docs/` prefix and the `how-to/` guides, read alongside until the feature pages replace them.

The sections, and the task each one covers:

| I want to ... | read ... |
|---|---|
| define, edit, run or hand off an agent | [agents](agents) |
| start, watch, control or message a session | [sessions](sessions) |
| create a workspace or work with its files | [workspaces](workspaces) |
| build or run a multi-step graph | [graphs](graphs) |
| store, find and read reference material | [knowledge](knowledge) |
| start work from a schedule, webhook or event | [triggers](triggers) |
| connect Slack, Telegram or Discord | [channels](channels) |
| ask a person or tell them something mid-turn | [human-in-the-loop](human-in-the-loop) |
| list, describe or call a toolset's tools | [toolsets](toolsets) |
| register a provider an agent runs on (needs an admin) | [providers](providers) |
| gate a tool call behind a person or a rule (needs an admin) | [approvals](approvals) |
| install a bundle of agents, graphs and documents | [harnesses](harnesses) |
| search the web, fetch a page or make a request | [web](web) |
| get the time, an id, a hash or a math answer | [utilities](utilities) |
| find what only the console or REST can drive | [not-agent-callable](not-agent-callable) |
| follow an end-to-end recipe | [cookbook](cookbook) |

Links in the wiki are wiki paths, never URLs: no leading slash, no `.md`, an optional `#anchor`. A link that goes nowhere is a defect, not a style.

The two kit tools divide the reading work:

- You have a wiki path, a section name, or you want the table of contents: `system__wiki_read` reads it. An empty path reads the contents.
- You only have words: `system__wiki_search` finds the pages, then you read one. It matches by meaning (its default) or by fulltext, and it says so in the result when it fell back.
- You want to know what exists on this install (an agent id, a collection, a profile): read the live map under [this-install](this-install) - `system__wiki_read` with a path prefix, or `system__wiki_search` with `path_prefix: "this-install"`.
- You want to know how to call a tool: the generated page under [reference/tools](reference/tools) is the tool's own schema, and `system__describe_tool` is the live answer.

## How to
1. Read [start-here](start-here) once; the other start-here pages are the contract for calling tools.
2. For a feature, open its section page from the table above; for a tool, open its reference page before you call it.
3. When you only have words, search first: `system__wiki_search` finds the pages, then `system__wiki_read` reads one.
4. Read a tool's reference page or call `system__describe_tool` before you call the tool: the reference page is the tool's own schema, and `system__describe_tool` is the live answer.

## Tool calls
`system__wiki_read` and `system__wiki_search` are core-kit tools, called directly by their scoped id (not through call_tool); `system__describe_tool` is not in the kit, so its call goes through the door (its own arguments name the tool by its toolset and its wire name).

### `system__wiki_read`
Reads one page by wiki path; an empty path reads the table of contents.

```json
{"name": "system__wiki_read", "arguments": {"path": "start-here/reading-this-wiki"}}
```

Reference: [`system__wiki_read`](reference/tools/system/wiki_read)

### `system__wiki_search`
Finds pages by words or by meaning; the result names the paths to read.

```json
{"name": "system__wiki_search", "arguments": {"query": "approval", "mode": "fulltext"}}
```

Reference: [`system__wiki_search`](reference/tools/system/wiki_search)

### `system__describe_tool`
Shows a tool's live schema, role and approval gate; the live answer for a tool you are about to call.

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "misc", "tool_name": "inform_user"}}
```

Reference: [`system__describe_tool`](reference/tools/system/describe_tool)

## Gotchas
- A path is a slug path: `start-here/reading-this-wiki`, not the file's `.md` and not a URL.
- `system__wiki_search` defaults to `mode: auto`; on a collection whose semantic indexing failed it falls back to fulltext and says so in the result. It does not invent hits.
- `system__wiki_search` excludes the live install map under `this-install/` by default so user-supplied descriptions do not crowd out platform reference pages; pass `path_prefix: "this-install"` (or a sub-prefix such as `"this-install/agents/"`) to search the live map.
- The generated reference pages are rebuilt from the code at boot: a page that disagrees with the live schema is a stale install, not a stale page.

## Related
[calling tools](start-here/calling-tools), [the platform wiki](knowledge/the-platform-wiki), [the core kit](start-here/the-core-kit).
