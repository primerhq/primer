---
title: "Open a file for the user"
summary: "Open a workspace file in the user's console without waiting."
order: 3
tools: [client__open_file]
related: [human-in-the-loop/inform-user, workspaces/in-session-file-tools]
---
## What it is
`client__open_file` asks the console attached to your session to open a workspace file, and returns at once. The result is `{"delivered": true}`. Delivery is best effort: with no console attached nothing opens, and the call still succeeds.

## Concepts
- `path` is the workspace-relative file to open; `line` is an optional 1-based line to scroll the viewer to.
- It is a direct call by its scoped id. The client toolset is not behind `system__call_tool`, so the door cannot reach it.
- The console grant is per turn: the call rides the turn of a session the caller already has open.
- Write the file first, with the session file tools, then open it: the viewer shows the file as it stands.
- Use it when you want the user to look at a file you just wrote or found: a report, a generated document, a log.

## How to
1. Write the file with the session file tools, or find the file that matters.
2. Open it in the console, with a `line` when one spot in it is the point.
3. Keep working: the call never waits for the viewer.

## Tool calls
### Open a file
Ask the attached console to open the file and return at once.

```json
{"name": "client__open_file", "arguments": {"path": "reports/digest.md", "line": 12}}
```

Reference: [`client__open_file`](reference/tools/client/open_file)

## Gotchas
- A missing console is not an error: nothing opens, and the result still says `{"delivered": true}`.
- It shows a file, not a fact: the words the user should read are `misc__inform_user`.

## Related
[inform the user](human-in-the-loop/inform-user), [in-session file tools](workspaces/in-session-file-tools).
