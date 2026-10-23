---
title: Calling tools
summary: "The call_tool contract, describe_tool and list_toolset_tools, and the tools that are called directly."
tools: [system__call_tool, system__describe_tool, system__list_toolset_tools]
related: [start-here/roles-and-approvals, start-here/errors]
---
## What it is
`system__call_tool` is the door to the tools of every built-in toolset: one tool, three arguments, and the inner tool's own result. The session file tools and `client__open_file` are not behind the door; they are called directly by their scoped id.

## Concepts
- The three arguments: a `toolset_id`, a `tool_name` (the wire id inside its toolset, as `system__list_toolset_tools` shows it) and an `arguments` object. The ids are scoped only on the wire: the toolset's own id and the bare wire id, never the combined `<toolset>__<tool>` spelling.
- Scoped ids: a direct tool is named `<toolset>__<tool>` (`system__call_tool`, `workspace__read`); a call through the door names the toolset and the tool separately.
- The result passes through unchanged: the dispatched tool's own output and error flag come back as `call_tool`'s result, wrapped in nothing.
- A pausing tool (one that parks the turn, like `system__ask_user`) parks under the `call_tool` call's id and answers that call when it resumes; the outer turn does not continue until it does.
- Reachable through the door: the ten built-in toolsets. The `workspace` and `client` scopes are direct-only.

## How to
1. Discover before you call: `system__describe_tool` shows a tool's schema and its live approval gate; `system__list_toolset_tools` lists a toolset's tools.
2. Call the tool through the door:

```json
{"toolset_id": "misc", "tool_name": "get_datetime", "arguments": {}}
```

3. Call the direct tools by their scoped id:

```json
{"name": "workspace__read", "arguments": {"path": "README.md"}}
```

```json
{"name": "client__open_file", "arguments": {"path": "README.md"}}
```

## Gotchas
- The arguments are the inner tool's arguments: no `principal`, no session id, nothing the inner schema does not name. An unknown key is ignored; a wrong type is a `validation-error`.
- The door looks the tool up in its toolset first: a name the toolset does not list is a `not-found` that names its tools, for every run. Then it checks the run's principal against the inner tool's role: a run below the floor is a `forbidden` before any approval gate. The roles are on [roles and approvals](start-here/roles-and-approvals).
- A gated inner tool parks the turn under the `call_tool` call, exactly as a direct gated call would.
- The reference pages under [reference/tools](reference/tools) carry the exact, validating call for every tool; read one before you invent arguments.

## Related
[roles and approvals](start-here/roles-and-approvals), [errors](start-here/errors).
