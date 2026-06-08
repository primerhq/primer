---
slug: sessions
title: Sessions — long-running workspace agent runs
summary: Headless agent execution inside a workspace, with file I/O, pause/resume control, and waiting.json state surfaces.
related: [workspaces, agents, yielding, chats]
mcp_tools:
  - system::list_sessions
  - system::get_session
  - system::create_session
  - system::update_session
  - system::delete_session
  - system::find_sessions
---

# Sessions — long-running workspace agent runs

## Overview

A **Session** is a headless agent run. Unlike a chat (which expects a
human in the loop), a session is started with an initial instruction
(or input) and runs to completion or pause without requiring user
interaction. Sessions live inside a **Workspace** — they share the
workspace's filesystem and git state with other sessions on the same
workspace. The workspace is what gives them a sense of place; the
session is the agent invocation inside it.

Sessions are the right primitive when the work is "do this thing,
take as long as you need, here are the tools, tell me when you're
done." Examples: code analysis runs that walk a repo, knowledge
ingestion that crawls a wiki, scheduled report generation that
pulls metrics. They expose pause and resume controls so an operator
can intervene without killing in-flight work, and they communicate
their blocked state through a per-session `waiting.json` file inside
the workspace.

Sessions are also where yields play their primary role. A session
can yield (via `ask_user`, `subscribe_to_trigger`, tool approval),
park in storage, and resume hours or days later — without holding
any compute resources in between. This is what makes "spin up a
session that waits for a trigger" cost-effective at scale.

## Mental model

A `Session` row carries:
- `id`, `workspace_id`, `agent_id` (or `graph_id` for graph sessions).
- `status` — `RUNNING | WAITING | PAUSED | ENDED`. High-level
  lifecycle position.
- `claimed_by` — worker id when running.
- `parked_status`, `parked_event_key`, `parked_until` — yield state
  (same fields as chats).
- `instruction` (optional) — the initial user-message-equivalent.

Per-session state lives at workspace-relative path
`.state/sessions/<session_id>/`. That subtree carries the LLM
message history (as commits to the workspace's git state repo), the
tool output cache, and `waiting.json` (when the session is paused
or waiting on external input).

Session tools (`ls`, `read`, `write`, `edit`, `glob`, `grep`,
`exec`) are composed onto the agent at session start — they're NOT
globally registered. Each session's tool dispatcher knows about
this session's workspace; the tools resolve paths relative to it.

`waiting.json` is the operator-facing description of why the session
is blocked. Two known shapes:
- `{"type": "user_input", "prompt": "..."}` — `ask_user` is parked.
- `{"type": "tool_approval", "tool": "...", "arguments": {...}}` —
  a required-type approval is parked.

The operator reads this to decide what to respond with.

## Lifecycle and states

`Session.status` transitions:

- `RUNNING` — currently claimed and executing.
- `WAITING` — parked on an external event (yield). Set when
  `parked_status="parked"`. The session won't be claimed by workers
  until the event fires.
- `PAUSED` — operator-requested pause via `request_pause()`. The
  worker finishes the current turn and stops; the session stays at
  this state until `request_resume()` lifts it.
- `ENDED` — terminal. Either the agent stopped, the operator ended
  it, or an unrecoverable error occurred. No further work runs.

Transitions:
- `RUNNING ↔ WAITING` — yield enters; resume exits.
- `RUNNING → PAUSED` — operator action; takes effect at next turn
  boundary.
- `PAUSED → RUNNING` — operator action.
- any → `ENDED` — terminal; not reversible.

The transitions are operator-driven via REST routes (also exposed
through the system toolset for agents to inspect):

- `request_pause(session_id)`
- `request_resume(session_id)`
- `request_end(session_id)`

The worker pool reads these flags between turns and acts on them.

Multi-session coordination: sessions on the same workspace can
share state via `.state/shared/` (a workspace-relative directory).
This is the primer-blessed channel for "agent A produces a file
that agent B reads." Tool calls in either session see the same
filesystem.

## MCP tools

Sessions appear in the system toolset as generic CRUD. The richer
operations (pause, resume, end, append instruction) are REST routes
not currently exposed as MCP tools — they're operator-facing.

- `system::list_sessions` — paginated.
- `system::get_session` — fetch the row including `status`, parked
  fields, and any error message.
- `system::create_session` — body needs `workspace_id`, `agent_id`,
  optional `instruction`. Creates the row and marks it claimable.
- `system::update_session` — partial update (rare; most updates
  happen via the workspace's `.state` repo).
- `system::delete_session` — terminal. Cancels the lease, removes
  the row, leaves the `.state/sessions/<id>/` subtree intact (it's
  workspace state, not session state).
- `system::find_sessions` — predicate query.

For starting a fresh session of a known agent in a known workspace,
the right tool is `system::create_session`. For triggering a fresh
session in response to an event, the right path is a trigger with
an `agent_fresh_session` subscription — see
[triggers-and-subscriptions](triggers-and-subscriptions.md).

## Workflows

### Workflow 1 — spin up a session and wait for it to finish

**Goal.** Run the `analyse-repo` agent against a known repo
checkout in workspace `ws-analysis-01`.

1. Create:

```json
{
  "tool": "system::create_session",
  "arguments": {
    "workspace_id": "ws-analysis-01",
    "agent_id": "analyse-repo",
    "instruction": "Walk the repo at /repo and produce a dependency report."
  }
}
```

Returns `{"id": "sid-abc", "status": "RUNNING", ...}`.

2. Poll until done:

```json
{
  "tool": "system::get_session",
  "arguments": {"id": "sid-abc"}
}
```

When `status=ENDED`, the session has finished. The output lives in
the workspace — tool outputs are in `.tmp/sid-abc/`, the LLM
history is in `.state/sessions/sid-abc/`, and any files the agent
wrote are wherever it wrote them.

### Workflow 2 — read why a session is stuck

**Goal.** Operator clicked into a `WAITING` session. They need to
know what it's waiting on.

1. Inspect status:

```json
{
  "tool": "system::get_session",
  "arguments": {"id": "sid-xyz"}
}
```

Returns `status=WAITING`, `parked_status="parked"`,
`parked_event_key="ask_user:sid-xyz:tc-42"`.

2. Read the `waiting.json` to see the prompt. (This requires
   workspace file access; if the operator isn't exposed
   `workspaces::read_workspace_file` they read it via REST.) The
   file body might be:

```json
{
  "type": "user_input",
  "prompt": "I found two possible config files (/etc/app.toml and /var/app/config.toml). Which is the live one?"
}
```

3. Operator answers by POSTing the reply (via the operator UI or
   a channel-forwarded reply). The session resumes.

## Gotchas

- **`waiting.json` is the contract.** It's how operators
  (and external automation) learn why a session is parked. Don't
  assume; read it. The shape is documented per yield type.
- **Session tools are NOT global.** `ls`, `read`, `write`,
  `exec` are composed onto the agent's tool set at session start.
  Code that lists "all tools" should expect different tool sets in
  different sessions on different workspaces.
- **Multi-session workspaces share filesystem.** Two sessions on
  `ws-X` can step on each other's files. `.state/shared/` is the
  blessed coordination directory. `.state/sessions/<id>/` is per-
  session.
- **Pause is between turns, not mid-LLM-call.** Setting pause
  finishes the current turn, then stops. Mid-tool-call: the tool
  completes; mid-stream tokens: the stream completes to the next
  stop boundary.
- **Ended is terminal.** A session that ended (clean, error, or
  operator-cancel) is not resumable. Create a fresh session if
  you want to continue from there.
- **Yield-cancellation differs from session-end.** Cancelling a
  yield produces a `tool_cancelled` result; the agent continues
  the turn. Ending the session stops the agent entirely.
- **`status=WAITING` and `parked_status="parked"` are redundant
  surface views of the same underlying fact.** The high-level
  status is what UIs display; the parked_status is what the worker
  pool checks for claim eligibility. Stay consistent when reading
  one; the other will agree.
- **Tool output caching is opt-in by size.** Tools whose output
  exceeds a threshold get cached to `.tmp/<sid>/` and the agent
  sees a preview + a hint to read the full output if needed.
  This protects context budget. Code that expects the full tool
  output inline gets surprised.

## Related

- [workspaces](workspaces.md) — sessions live inside workspaces;
  workspaces own the filesystem + git state.
- [agents](agents.md) — the agent defines what a session does;
  the session is one instance of running an agent.
- [yielding](yielding.md) — yields are how sessions transition
  to WAITING.
- [chats](chats.md) — the human-in-the-loop sibling of sessions;
  same yield mechanics, different lifecycle.
