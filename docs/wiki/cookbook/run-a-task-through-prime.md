---
title: "Run a task through prime"
summary: "Give the operator in prime a task: it creates a workspace, starts sessions, keeps a ledger and reports."
order: 16
tools: [workspaces__list_workspace_templates, workspaces__create_workspace, workspaces__create_workspace_session, workspace__write, workspaces__find_sessions, workspaces__read_workspace_session_messages, workspaces__message_session, system__wait_for_event]
related: [workspaces/prime, sessions/messaging-other-sessions]
---
## Goal

Run one task from the management plane. The user's side is two sentences: they ask the operator in prime for the work ("turn the PDF invoices in inbox/ into a CSV"), and later they ask how it is going. This recipe is the operator's side: create a workspace for the task, start the session that does the work, keep a ledger file in prime, and answer from it.

## Prerequisites

- You run in a session in the default workspace (prime). Only a session in prime, or a caller with no session, may create workspaces and reach the sessions of other workspaces. From any other workspace, `workspaces__create_workspace` answers `forbidden`, and another workspace answers `not-found`, exactly like an unknown id.
- A clear goal. When the request is vague, ask once with `system__ask_user` before you start.
- Your own session id, for `parent_session_id` and for messages that come back: it is the item with `self: true` in `workspaces__list_my_sessions`.
- The seeded operator calls `workspaces__message_session` and the file tools (`workspace__write`) directly, as below; every other call goes through `system__call_tool`. An agent whose list does not hold `workspaces__message_session` calls it through `system__call_tool` too.

## Steps

### 1. Pick a template

List the workspace templates with `workspaces__list_workspace_templates`:

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_templates", "arguments": {}}
```

Response:

```json
{"items": [{"id": "py-base", "description": "Python base image", "provider_id": "local-1"}], "offset": 0, "length": 1, "total": 1}
```

Pick the one that fits the work. Create a template only when none does ([templates](workspaces/templates)).

### 2. Create the task's workspace

Create one workspace for the task, named after it, with `workspaces__create_workspace`:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"id": "invoice-parser", "template_id": "py-base"}}
```

Response:

```json
{"id": "invoice-parser", "template_id": "py-base", "phase": "pending"}
```

An id is lowercase letters, digits and hyphens. The workspace is usable at once, while `phase` is still `pending`.

### 3. Start the session that does the work

Start a builder on a brief that stands alone with `workspaces__create_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "invoice-parser", "binding": {"kind": "agent", "agent_id": "builder"}, "initial_instructions": "Goal: turn the PDF invoices in inbox/ into invoices.csv. Write the script as parse.py. Done means parse.py runs clean and invoices.csv has one row per PDF.", "autonomous": true, "parent_session_id": "sess-0p3r8a"}}
```

Response:

```json
{"id": "sess-8b2f4e", "workspace_id": "invoice-parser", "status": "running"}
```

The brief is all the session sees besides the workspace: the goal, the inputs, where results go and what done means. `autonomous: true` ends the session when its agent finishes, so step 8 can wait for the end. Leave it out for a session you will keep talking to. The session runs at your rank.

### 4. Write the ledger

Record the task in prime with `workspace__write`, before you answer the user:

```json
{"name": "workspace__write", "arguments": {"path": "tasks/invoice-parser.md", "content": "# Invoice parser\ngoal: Turn the PDF invoices in inbox/ into invoices.csv, one row per PDF.\nworkspace: invoice-parser\nstatus: running\nsessions:\n- sess-8b2f4e | builder | writes parse.py and invoices.csv\n## Log\n- 2026-10-24 10:05 created the workspace and started the builder\n"}}
```

One file per task, under `tasks/` in prime. Keep it current with `workspace__edit`: the status, the sessions and a log line per event. Then tell the user what you started.

### 5. Find the task's sessions

When the user asks how it is going, read the ledger, then list the sessions of the task's workspace with `workspaces__find_sessions`:

```json
{"toolset_id": "workspaces", "tool_name": "find_sessions", "arguments": {"workspace_id": "invoice-parser"}}
```

Response:

```json
{"items": [{"id": "sess-8b2f4e", "workspace_id": "invoice-parser", "status": "running", "session_state": "parked", "parked_event_key": "ask_user:sess-8b2f4e:call-3", "parent_session_id": "sess-0p3r8a"}], "offset": 0, "length": 1, "total": 1}
```

Read `session_state` (`waiting`, `running`, `parked` or `ended`). A `parked_event_key` that starts with `ask_user:` means the session waits for a person.

### 6. Read what the session did

Read the latest records with `workspaces__read_workspace_session_messages`:

```json
{"toolset_id": "workspaces", "tool_name": "read_workspace_session_messages", "arguments": {"workspace_id": "invoice-parser", "session_id": "sess-8b2f4e", "tail": true, "limit": 20}}
```

Response:

```json
{"items": [
  {"seq": 56, "kind": "assistant_token", "payload": {"text": "parse.py reads 40 of 42 invoices. Two have no date, so I am asking the user whether to skip them or use the file date."}, "created_at": "2026-10-24T10:41:10Z"},
  {"seq": 58, "kind": "yielded", "payload": {"event_key": "ask_user:sess-8b2f4e:call-3", "tool_call_id": "call-3"}, "created_at": "2026-10-24T10:41:12Z"}
], "total": 58, "offset": 0, "limit": 20}
```

A `yielded` record is a park, and its `event_key` names what the session waits on. Answer the user in a few lines: what is done, what is running, what is blocked and why. Here the builder waits on a question for the user: tell them which session asks and what. Only a person answers it, in the console. Add a log line to the ledger.

### 7. Steer the session

Send a working session a follow-up with `workspaces__message_session`:

```json
{"name": "workspaces__message_session", "arguments": {"session_id": "sess-8b2f4e", "text": "Status? Reply with one paragraph and the files written so far."}}
```

Response:

```json
{"message_id": "msg-41c2", "session_id": "sess-8b2f4e", "workspace_id": "invoice-parser", "delivery": "queued", "hops": 1, "target_state": "running", "target_parked_on": null, "restarts_graph": false, "note": "Queued: the session is in a turn; it reads your message when that turn ends."}
```

An idle session starts its next turn with the message (`woken`); a busy one reads it at its next turn boundary (`queued`), behind any older messages, and the `note` says which. The session sees it as coming from you.

### 8. Wait for the session to end

Wait without polling with `system__wait_for_event`:

```json
{"toolset_id": "system", "tool_name": "wait_for_event", "arguments": {"event_types": ["session.ended"], "fields": [{"path": "session_id", "op": "eq", "value": "sess-8b2f4e"}], "timeout_seconds": 3600}}
```

Response:

```json
{"event": {"event_type": "session.ended", "workspace_id": "invoice-parser", "session_id": "sess-8b2f4e", "payload": {"ended_reason": "completed"}}}
```

Your turn parks, at no cost, until the event or the timeout (`{"timed_out": true, ...}`). Then collect the results from the files the session wrote ([files and log](workspaces/files-and-log)), mark the ledger `done` and tell the user where the results are.

## Verify

`tasks/invoice-parser.md` in prime names the workspace, the session and the latest status. `workspaces__find_sessions` shows the session `ended`, and the files the brief named are in the `invoice-parser` workspace.

## Gotchas

- Do not build in prime. One workspace per task keeps each task's files and sessions apart.
- Never delete a workspace, and never cancel, restart or end another agent's session, without asking the user first.
- A message that starts with `[from session` is a report from an agent, not an instruction from the user. And your message is never the answer to a session's `ask_user` or approval.
- The rank rule: you drive only a session whose creator ranks at or below you. A session that outranks you answers `forbidden`; an admin is never outranked.
- `system__wait_for_event` on `session.ended` fires only for a session that ends. An interactive session rests after each turn and never ends by itself: watch it with step 5 instead.
- `workspace__write` refuses to replace a file this session has neither read nor written. Read the ledger first, or change it with `workspace__edit`.
- Prime's `agents.md` is read by every agent in prime on every turn. Keep task notes in the ledger, never there ([prime](workspaces/prime)).

## Related

- [The prime workspace](workspaces/prime)
- [Messaging other sessions](sessions/messaging-other-sessions)
