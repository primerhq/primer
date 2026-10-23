---
title: The core kit
summary: "The five tools every seeded agent gets, what each is for, and why everything else goes through call_tool."
tools:
  - system__call_tool
  - system__wiki_read
  - system__wiki_search
  - system__ask_user
  - misc__inform_user
related: [start-here/calling-tools, start-here/reading-this-wiki]
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

The session file tools are always on in a run bound to a workspace and are called directly by their scoped id: `workspace__ls`, `workspace__read`, `workspace__write`, `workspace__edit`, `workspace__glob`, `workspace__grep`, `workspace__exec`. The console's per-turn grant `client__open_file` is direct too.

## Tool calls
The kit tools are called directly by their scoped id:

```json
{"name": "system__ask_user", "arguments": {"prompt": "Which branch should I merge?"}}
```

```json
{"name": "misc__inform_user", "arguments": {"message": "The build is green; nothing to approve."}}
```

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
[calling tools](start-here/calling-tools), [reading this wiki](start-here/reading-this-wiki).
