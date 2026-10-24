---
title: "The core kit"
summary: "The five tools every seeded agent carries and why everything else goes through call_tool."
order: 5
tools: [system__call_tool, system__wiki_read, system__wiki_search, system__ask_user, misc__inform_user]
related: [start-here/calling-tools, agents/tools-and-grants, human-in-the-loop/ask-user]
---
## What it is
A seeded agent's run starts with the five core-kit tools, granted directly instead of through the door (the seeded operator also holds two session-messaging tools). Everything else the platform offers is reachable through `system__call_tool`.

## Concepts
The five tools, in the order the kit grants them:

- `system__call_tool` ([reference](reference/tools/system/call_tool)) - the door: it runs any tool of a built-in toolset, with the run's own principal and context, and passes the inner result through unchanged.
- `system__wiki_read` ([reference](reference/tools/system/wiki_read)) - reads one wiki page by path; an empty path reads the table of contents.
- `system__wiki_search` ([reference](reference/tools/system/wiki_search)) - finds wiki pages by words or by meaning.
- `system__ask_user` ([reference](reference/tools/system/ask_user)) - parks the turn until the operator answers; the answer comes back as the call's result.
- `misc__inform_user` ([reference](reference/tools/misc/inform_user)) - a one-way status message to the operator: no reply is awaited and the run continues immediately.

The kit is small on purpose: an agent should not carry 200 tool definitions in its context at every turn. The door plus the wiki gets it the rest: search the wiki, read the tool's reference page for the exact call, and run the tool through the door.

The session file tools are always on in a run bound to a workspace and are called directly by their scoped id: `workspace__ls`, `workspace__read`, `workspace__write`, `workspace__edit`, `workspace__glob`, `workspace__grep`, `workspace__exec`. The console's per-turn grant `client__open_file` is direct too.

## How to
1. When you need a tool that is not in the kit, find its page: `system__wiki_search` for the words, `system__wiki_read` for the path.
2. Read the tool's reference page for the exact call and its role, or call `system__describe_tool` through the door for the live answer.
3. Run the tool through the door (`system__call_tool`); hold the turn for the operator with `system__ask_user`, and send progress with `misc__inform_user`.

## Tool calls
The five kit tools are called directly by their scoped id.

### `system__call_tool`
The door, shown in its direct form around an inner call: any tool of any toolset you can reach.

```json
{"name": "system__call_tool", "arguments": {"toolset_id": "misc", "tool_name": "get_datetime", "arguments": {}}}
```

Reference: [`system__call_tool`](reference/tools/system/call_tool)

### `system__wiki_read`
Reads one wiki page by path; an empty path reads the table of contents.

```json
{"name": "system__wiki_read", "arguments": {"path": "start-here/the-core-kit"}}
```

Reference: [`system__wiki_read`](reference/tools/system/wiki_read)

### `system__wiki_search`
Finds wiki pages by words or by meaning; the result names the paths to read.

```json
{"name": "system__wiki_search", "arguments": {"query": "approval", "mode": "fulltext"}}
```

Reference: [`system__wiki_search`](reference/tools/system/wiki_search)

### `system__ask_user`
Parks the turn until the operator answers; the answer comes back as the call's result.

```json
{"name": "system__ask_user", "arguments": {"prompt": "Which branch should I merge?"}}
```

Reference: [`system__ask_user`](reference/tools/system/ask_user)

### `misc__inform_user`
A one-way status message to the operator: no reply is awaited and the run continues immediately.

```json
{"name": "misc__inform_user", "arguments": {"message": "The build is green; nothing to approve."}}
```

Reference: [`misc__inform_user`](reference/tools/misc/inform_user)

Everything else goes through the door:

```json
{"toolset_id": "crud", "tool_name": "create_agent", "arguments": {"entity": {"description": "A builder agent.", "model": {"profile_id": "my-profile"}}}}
```

## Gotchas
- `system__ask_user` parks the turn: the agent holds no result until the operator answers, and a parked run releases its worker lease until the answer arrives.
- `misc__inform_user` never answers: it is for progress, not for questions. A question is `system__ask_user`.
- A tool that is not in the kit and not a session file tool is not a direct call: name it through `system__call_tool`, or the model's call has no target.
- For the builder, planner, explorer and tool-runner, the kit is the whole direct grant (the operator adds only `workspaces__list_my_sessions` and `workspaces__message_session`): a seeded agent that needs another tool gets it through the door, not by widening the kit.

## Related
[calling tools](start-here/calling-tools), [reading this wiki](start-here/reading-this-wiki), [tools and grants](agents/tools-and-grants), [ask user](human-in-the-loop/ask-user).
