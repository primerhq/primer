---
slug: sessions
title: Sessions - long-running workspace agent runs
summary: Headless agent execution inside a workspace, with file I/O, pause/resume control, and waiting.json state surfaces.
related: [workspaces, agents, yielding, tool-approval, graphs]
mcp_tools:
  - workspaces::create_workspace_session
  - workspaces::get_workspace_session
  - workspaces::read_workspace_session_messages
  - workspaces::list_workspace_sessions
  - workspaces::pause_workspace_session
  - workspaces::resume_workspace_session
  - workspaces::steer_workspace_session
  - workspaces::interrupt_workspace_session
  - workspaces::cancel_workspace_session
---

# Sessions - long-running workspace agent runs

## Overview

A **Session** is a headless agent run, started with
`workspaces::create_workspace_session` and polled with
`workspaces::get_workspace_session`.
A session is started with an initial instruction (or input) and runs
to completion or pause without requiring user interaction; a person can
steer it while it runs, but nothing waits on one unless the agent asks. Sessions live inside a **Workspace** - they share the
workspace's filesystem and git state with other sessions on the same
workspace. The workspace is what gives them a sense of place; the
session is the agent invocation inside it.

Creating and cancelling a session are first-class MCP tools
(`workspaces::create_workspace_session` /
`workspaces::cancel_workspace_session`), so an external MCP-connected
agent can run an agent or graph headlessly end to end.

Use a session when the work runs headless to completion with no
human in the loop; not when you need a back-and-forth conversation
with a person (steer the session over REST, not MCP).

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
park in storage, and resume hours or days later - without holding
any compute resources in between. This is what makes "spin up a
session that waits for a trigger" cost-effective at scale.

## Mental model

A `Session` row carries:
- `id`, `workspace_id`, `agent_id` (or `graph_id` for graph sessions).
- `status` - `CREATED | RUNNING | WAITING | PAUSED | ENDED`.
  High-level lifecycle position.
- `claimed_by` - worker id when running.
- `parked_status`, `parked_event_key`, `parked_until` - yield state
  (see yielding for what sets them).
- `parked_tool_batches`, `resumable_at` - internal park bookkeeping
  (a marker for parks holding tool calls run as claims, and when the
  park was last woken); do not act on them.
- `instruction` (optional) - the initial user-message-equivalent.

Per-session state lives at workspace-relative path
`.state/sessions/<session_id>/`. That subtree carries the LLM
message history (as commits to the workspace's git state repo) and
`waiting.json` (when the session is paused or waiting on external
input). Large tool outputs are cached separately under
`.tmp/<session_id>/`.

Session tools (`ls`, `read`, `write`, `edit`, `glob`, `grep`,
`exec`) are composed onto the agent at session start - they're NOT
globally registered. Each session's tool dispatcher knows about
this session's workspace; the tools resolve paths relative to it.

`waiting.json` is the operator-facing description of why the session
is blocked. Two known shapes:
- `{"type": "user_input", "prompt": "..."}` - `ask_user` is parked.
- `{"type": "tool_approval", "tool": "...", "arguments": {...}}` -
  a required-type approval is parked.

The operator reads this to decide what to respond with.

## auto_start and explicit start

Every session is created with `status=CREATED`. The `auto_start` flag
on `workspaces::create_workspace_session` (REST: `POST
/v1/workspaces/{id}/sessions`) controls whether the session begins
running immediately or stays inert until explicitly started.

- **`auto_start=true` (MCP tool default)**: the session transitions to
  `RUNNING`, the scheduler enqueues it, and a worker picks it up as
  soon as one is free. The create call returns `status=running`.
- **`auto_start=false` (REST API default)**: the session stays in
  `CREATED` with no lease registered. No worker will claim or run it.
  The session remains inert indefinitely until you explicitly start it
  with `workspaces::resume_workspace_session` (REST: `POST
  .../sessions/{id}/resume`). That call transitions `CREATED -> RUNNING`
  and registers the lease so a worker picks it up.

Use `auto_start=false` when you need to set something up between
creating the session and letting a worker run it, or when you want to
create sessions in batches and start them on demand.

## Lifecycle and states

`Session.status` transitions:

- `RUNNING` - currently claimed and executing.
- `WAITING` - parked on an external event (yield). Set when
  `parked_status="parked"`. The session won't be claimed by workers
  until the event fires.
- `PAUSED` - operator-requested pause via `request_pause()`. The
  worker finishes the current turn and stops; the session stays at
  this state until `request_resume()` lifts it.
- `ENDED` - terminal. Either the agent stopped, the operator ended
  it, or an unrecoverable error occurred. No further work runs.
  A transient failure of the model call (a provider 5xx, a rate limit,
  a dropped connection, a stalled or too-slow generation) does NOT end an
  interactive session once the retries are spent: it rests `WAITING`, the
  row's `last_turn_error` (`{code, at}`) says the turn failed, and your
  next message continues the same session (a stream that died without
  any error code rests the same way). A graph, a trigger- or
  webhook-started session, a rejected request (bad credentials, a bad
  request, an overflowing context), a code nobody classified and a crash
  still end it. Watch
  `session.turn_failed` (`{code, ended}`) for failed turns, not only
  `session.ended`.

Transitions:
- `RUNNING ↔ WAITING` - yield enters; resume exits.
- `RUNNING → PAUSED` - operator action; takes effect at next turn
  boundary.
- `PAUSED → RUNNING` - operator action.
- any → `ENDED` - terminal; not reversible.

The transitions are driven via REST routes and the matching
`workspaces` MCP tools:

- `request_pause(session_id)` / `workspaces::pause_workspace_session`
- `request_resume(session_id)` / `workspaces::resume_workspace_session`
- `request_end(session_id)` / `workspaces::cancel_workspace_session`

The worker pool reads these flags between turns and acts on them.

Every session row also serves `session_state`, one derived value
(`waiting`, `running`, `parked` or `ended`) computed from those fields.
`running` means a turn is in flight right now; `parked` covers a session
parked on a yielding tool and one resting between turns. To list the
sessions that are working at this moment, ask for
`GET /v1/sessions?session_state=running`, not `status=running`, which also
returns a session parked on a yielding tool and one queued for a worker that
has not claimed it yet. The filters AND together.

Stop is a separate control that none of the flags above is: it stops the
turn a session is running and leaves the session alive in `WAITING`
(`POST .../sessions/{session_id}/interrupt` and
`workspaces::interrupt_workspace_session`; see "MCP tools" below).

## Where your session actually runs

A session is not pinned to the process that created it. Under the
remote-worker / Kubernetes topology, primer runs a pool of workers
(possibly across many processes, and in k8s across many pods), and any
free worker can claim the session. The worker that runs the first turn
may not be the one that runs the next, and the process that handled
`create_workspace_session` may never touch the session again. After a
park-and-resume, a different worker entirely usually picks it back up.

The practical consequence: do not assume locality. Drive a session
through its durable, storage-backed surface - poll
`workspaces::get_workspace_session` (or the REST endpoints) for state;
do not hold a handle and expect the originating process to still own the
work. State lives in storage and in the workspace's git-backed
`.state/`, both of which any worker reads, which is exactly what lets a
parked session resume on whatever worker is free hours or days later.

Multi-session coordination: sessions on the same workspace can
share state via `.state/shared/` (a workspace-relative directory).
This is the primer-blessed channel for "agent A produces a file
that agent B reads." Tool calls in either session see the same
filesystem.

## MCP tools

Sessions live in the `workspaces` toolset. Creating, inspecting, and
controlling a session are all exposed over MCP, so an external agent
can run an agent or graph headlessly end to end.

- `workspaces::create_workspace_session` - start a session bound to
  an agent or graph. Args: `workspace_id`, `binding`
  (`{kind:"agent", agent_id}` or `{kind:"graph", graph_id}`),
  optional `initial_instructions`, `auto_start` (default true),
  optional `graph_input`, `parent_session_id`, `metadata`. Returns
  the session row with its `id` and `status`.
- `workspaces::get_workspace_session` - fetch the row including
  `status`, parked fields, and any error message. Args:
  `workspace_id`, `session_id`.
- `workspaces::list_workspace_sessions` - list sessions on a
  workspace.
- `workspaces::pause_workspace_session` /
  `workspaces::resume_workspace_session` /
  `workspaces::steer_workspace_session` - control a running session
  (pause at the next turn boundary, resume, or inject steering input).
- `workspaces::interrupt_workspace_session` - Stop the turn a session is
  running without ending the session. Args: `workspace_id`, `session_id`.
  Returns the session row, and **a success answer alone is not a Stop**:
  `interrupt_requested: true` means a Stop was recorded;
  `interrupt_requested: false` means nothing was running (the session was
  idle, paused or not started), nothing was recorded and nothing will
  stop. `type=conflict` when the session has ended or is parked waiting on
  an approval, an answer or a timer (cancel it instead);
  `type=not-found` for an unknown session. It is the same operation as the
  console's Stop button, one implementation behind both. It does not end
  the session, so a parent waiting for a stopped child to END waits on:
  use `workspaces::cancel_workspace_session` to end one.
- `workspaces::cancel_workspace_session` - hard-cancel a session.
  Args: `workspace_id`, `session_id`. Returns the session
  `{status:"ended", ended_reason:"cancelled"}` (or still running with
  `cancel_requested` while an in-flight turn is preempted). A turn
  blocked in a long model or tool call is preempted within about the
  worker heartbeat interval (10 seconds by default), even if the fast
  cancel signal was lost on the way. This is
  how you end a session; sessions are not a CRUD entity and there is
  no delete.

Stopping a turn without ending the session is the console's Stop button
(`POST /v1/workspaces/{workspace_id}/sessions/{session_id}/interrupt`) and
`workspaces::interrupt_workspace_session`, one implementation behind both.
The session lands in `waiting` with its history intact, so the next
message continues it:

- It takes effect at the next wait for the model: before the first token
  as well as between chunks. If the model had just asked for tool calls
  when you pressed Stop, none of them run: each is answered "not run:
  stopped by user".
- A tool call that is already running when you press Stop is stopped too.
  The call is cancelled and given up to 3 seconds to unwind (so whatever
  cleanup the tool does when it is cancelled has run before the turn moves
  on, and a subagent has stopped; what stops with a command is described in
  the workspaces doc), and it is answered "interrupted: stopped by
  user (the call may have run, and its result was not recorded)". A call
  that finishes before the Stop, at the same moment, or while the Stop is
  being handled keeps its real result: a Stop never throws a real result
  away. The calls after it in the same batch that have not started do not
  run: each is answered "not run: stopped by user", and the turn ends
  before the next model call.
- A few tools are not cancelled, because cancelling them could leave work
  half done: a file write (`write`, `edit`, `write_workspace_file`,
  `delete_workspace_file`) on a local workspace runs in a thread under the
  workspace lock, so cancelling the wait would release the lock while the
  write still runs (on a container workspace a cancel cannot undo a write
  already sent, so it would only throw away the real result), and the
  document tools (`create_document`, `update_document`,
  `move_document`, `delete_document` in the collections toolset, and
  `put_document`, `create_document` and `delete_document` in the system
  toolset) commit a database transaction and then update the search
  index, so cancelling between the two would leave the index stale. So are
  the tools that change more than one thing, each with no transaction around
  the steps: creating, updating, deleting or firing a trigger (the row, then
  its schedule), creating or deleting a workspace (the live instance, then
  its row), creating, cancelling, steering or restarting a workspace session,
  and updating or deleting an LLM, embedding, cross-encoder or
  semantic-search provider or a toolset (the row, then the cached adapter
  built from it). A tool called through `call_tool` is judged by the tool it
  calls: `call_tool` of a tool above is waited for too. A Stop
  waits for such a call, up to 5 seconds, and records its real result. If it
  takes longer the turn ends anyway: the call is answered "interrupted:
  stopped by user ..." and runs on to its end in the background, so it can
  still complete after the Stop. An operator Cancel of the session cancels
  every call, whatever it declares: a workspace create cancelled that way
  removes the instance it made, and a workspace delete cancelled that way
  still runs to its end. Each tool declares this with the
  `interruptible` flag (false for the tools above, true for every other
  tool). It is served next to `yields` wherever tools are listed: the
  `interruptible` field of each tool in `GET /v1/tools` and the toolset
  tool listings, and the MCP exposure table.
- A call that is cancelled never gets to ask the session to park. A call
  that asks to park at the very moment of the Stop is handled as follows. If
  it waits on no person (a timer, a trigger, a remote task, a batch of
  tool calls handed to workers), the Stop ends the turn instead: the session
  lands in `waiting` and every call of that round is answered in the
  transcript (the calls that finished before the one that asked to wait keep
  their real results, "interrupted: stopped by user ..." for the call that
  asked to wait, whose result was not recorded, and "not run: stopped by user"
  for the ones after it). A pending call to a tool you supplied yourself
  (`external_tool`) is cancelled too, so it no longer shows as pending,
  whether the Stop ended its park or cancelled the call before it parked.
  What else that tool already started (a remote task it submitted, a timer it
  set) is not cancelled. If it is
  asking a person (a tool approval, an `ask_user` question), the session
  parks as before and the Stop is dropped, because the answer wins; Stop is
  then refused with 409 and Cancel is the way out. Calls later in the batch
  are refused, so they cannot park. Cancel refuses the calls that have not
  started in the same way (and then ends the session). A call that needs
  sign-in (an MCP tool asking for consent) when the Stop lands is answered
  "interrupted" instead of sending you to the consent page. A subagent call
  (`invoke_agent`) that is cancelled and does not stop in time is given up
  on: what it, and any subagent it started in turn, still emits is not
  added to the session log after the call's "interrupted" answer. Giving up
  hides the subagent's log records; it does not stop it. A subagent is
  given the same Stop, so it ends at its own next check (before its next
  model call and before each of its tool calls), including one cut off
  while it connects to a stdio MCP server, and its partial answer is not
  handed back as a result: the call is answered "interrupted". A subagent
  that is already inside a tool call that does not stop can finish that one
  call in the background, and what it does is not undone; a subagent that
  was resumed after a park does not get the Stop yet. Graph sessions and the context-compaction call that can run first are not
  interruptible yet.
- What the model had already written is kept in the transcript. The model
  itself does not see that partial text on the next turn, and is not told
  it was stopped (apart from the "not run: stopped by user" results
  above); tool rounds that completed are kept.
- The request is recorded on the session row (`interrupt_requested`) and,
  while the turn is running, the worker re-reads it every 2 seconds (one
  point read per running turn), so a Stop is delayed, not lost, if the
  fast signal fails. The row shows `interrupt_requested: true` until the
  turn stops.
- A session that is parked (waiting on an approval, an answer or a timer)
  has no turn to stop: the request is refused with 409 and nothing is
  recorded; use Cancel to end it. A session whose park has just fired and
  is resuming is refused too ("the session is resuming; Stop is not
  available during a resume"). And a later human action wins over an
  earlier Stop: approving or answering a park, or sending a message to a
  session that is not running a turn, clears a Stop that was pressed
  before.
- A session may stop itself with the tool, and so may a subagent running
  inside it (a subagent shares the outer session's id and is given the
  same Stop). The call is part of the turn it stops: it usually keeps its
  real result (a Stop never throws a real result away; if the Stop wins
  the race the call is answered "interrupted: stopped by user"), the calls
  after it in the same round do not run (each is answered "not run:
  stopped by user"), a subagent ends at its next check and the call to it
  is answered "interrupted", and the turn stops before the next model
  call. Know three things before an agent does this: an autonomous session
  lands in `waiting` and stays there until a message wakes it; a graph
  session stops only between graph events, not at the next model call; and
  a parent waiting for a stopped child to end waits on, because the child
  is alive (cancel ends it).

For starting a fresh session of a known agent in a known workspace,
the right tool is `workspaces::create_workspace_session`. For
triggering a fresh session in response to an event, the right path is
a trigger with an `agent_fresh_session` subscription - see
[triggers-and-subscriptions](triggers-and-subscriptions.md).

## Workflows

### Workflow 1 - spin up a session and wait for it to finish

**Goal.** Run the `analyse-repo` agent against a known repo
checkout in workspace `ws-analysis-01`.

1. Create:

```json
{
  "tool": "workspaces::create_workspace_session",
  "arguments": {
    "workspace_id": "ws-analysis-01",
    "binding": {"kind": "agent", "agent_id": "analyse-repo"},
    "initial_instructions": "Walk the repo at /repo and produce a dependency report.",
    "auto_start": true
  }
}
```

Returns the session with its `id` and a `status` of `running`: `{"id": "sid-abc", "status": "running"}`. Thread the `id` into the poll below.

2. Poll until done:

```json
{
  "tool": "workspaces::get_workspace_session",
  "arguments": {"workspace_id": "ws-analysis-01", "session_id": "sid-abc"}
}
```

When `status` is `ended`, the session has finished. Read what it said
with `workspaces::read_workspace_session_messages` (`{workspace_id,
session_id}`, optional `after_seq` / `limit` / `tail`): the same
transcript `GET /v1/sessions/{id}/messages` serves. Any files the agent
wrote are wherever it wrote them (`workspaces::read_workspace_file`).
The runtime keeps the transcript under `.state/sessions/sid-abc/` and
large tool outputs under `.tmp/sid-abc/`, but raw reads of `.state/`
and `.tmp/` are admin-only; use the transcript tool instead.

### Workflow 2 - read why a session is stuck

**Goal.** Operator clicked into a `WAITING` session. They need to
know what it's waiting on.

1. Inspect status:

```json
{
  "tool": "workspaces::get_workspace_session",
  "arguments": {"workspace_id": "ws-analysis-01", "session_id": "sid-xyz"}
}
```

Returns `status="waiting"`, `parked_status="parked"`,
`parked_event_key="ask_user:sid-xyz:tc-42"`.

2. Read the prompt with `GET /v1/sessions/{session_id}/ask_user/pending`
   (below), or read the session's latest records with
   `workspaces::read_workspace_session_messages` (`tail: true`). Do not
   read `.state/sessions/<id>/waiting.json` directly: raw `.state` reads
   are admin-only. The pending prompt might be:

```json
{
  "type": "user_input",
  "prompt": "I found two possible config files (/etc/app.toml and /var/app/config.toml). Which is the live one?"
}
```

3. Operator answers by POSTing the reply (via the operator UI, a
   channel-forwarded reply, or the REST resume endpoints below). The
   session resumes.

These are the REST endpoints for inspecting and answering a parked
session, all rooted at `/v1/sessions/{session_id}`:

```text
GET  /v1/sessions/{session_id}/ask_user/pending
POST /v1/sessions/{session_id}/ask_user/respond   {"tool_call_id": "...", "response": ...}
POST /v1/sessions/{session_id}/yields/{tool_call_id}/cancel
```

`ask_user/pending` returns the `tool_call_id`, the `gate_id` (the id of THIS
prompt, minted when it was asked; `null` for a prompt parked before gates had
ids), the `prompt`, and the optional `response_schema` (404 when the session is
not parked on an `ask_user`); `ask_user/respond` validates the `response` against that
schema and resumes the session. Send the `gate_id` back
(`{"tool_call_id": ..., "gate_id": ..., "response": ...}`): the provider repeats its
`tool_call_id` across rounds, so an answer meant for an earlier question could
otherwise answer a later one under the same id. A `gate_id` that is no longer the
pending prompt's is a `409` with `extensions.code = "approval_stale"` and nothing is
resumed; a malformed one is a `422`; one left out is still accepted (counted in
`gate_respond_total{kind="ask_user",gate_token="absent"}`). The cancel route takes the
same optional `gate_id` in its body; `yields/{tool_call_id}/cancel` skips one
in-flight yield without ending the session (the tool sees a cancelled
result and the agent's turn continues). On a call to a tool you supplied
yourself (`external_tool`) the cancel also marks the call's record
`cancelled` (`GET /v1/external_tool_calls`), unless the call already
ended: a call record keeps the FIRST status it ends in (`completed`,
`cancelled` or `timed_out`), so a cancel, a session cancel or a timeout
that comes after your result never overwrites `completed`. The record is
an audit trail, not what the session executed: a result you sent can be
accepted (the steer answers 2xx and the agent receives it) while its
record still ends `cancelled` or `timed_out`, when a cancel, a session
cancel, delete or restart, a Stop or the record's timeout reached the
record between the session taking your result and the record being
written. The session's transcript is what the agent actually got. A
result holding NaN or Infinity (Python's `json` module writes them by
default) is delivered to the agent and recorded as `null`. The same
endpoints answer a graph session parked mid-run on an `ask_user` node (see
[graphs](graphs.md)). Tool-approval parks have their own pair
(`tool_approval/pending` + `tool_approval/respond`); see
[tool-approval](tool-approval.md).

## Gotchas

- **`auto_start=false` leaves the session truly inert.** No worker
  will touch it until you call resume. There is no polling or timeout
  that will auto-start it. If you create a session with
  `auto_start=false` and never resume it, it stays `CREATED` forever
  (until cancelled or deleted).
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
- **A successful `interrupt_workspace_session` call is not a Stop.** It
  answers with the session row even when nothing was running (an idle,
  paused or not-started session). Read `interrupt_requested` in the
  answer: `true` means a Stop is recorded, `false` means nothing was
  recorded. Stop leaves the session alive; to end it, cancel.
- **Ended is terminal.** A session that ended (clean, error, or
  operator-cancel) is not resumable. Create a fresh session if
  you want to continue from there.
- **A failed model call's error record says which provider and what it said.** The `error` record's `message` names the provider by its
  configured id and kind (`Model provider 'lm-studio-box' (openchat/lmstudio) had a server error (HTTP 500): ...`) and carries the provider's
  own text with credentials masked, control characters stripped and the text capped at 300 characters; that rewrite is for four shapes only
  (server error, rate limit, authentication, network). Any other message (a rejected request, an error in the middle of a stream) keeps the
  SDK's own wording, masked and capped at 4000 characters. Branch on the record's `code`, not on the sentence: a failure in a stream carries the
  classification (`server_error`, `rate_limit`, `auth_error`, `network_error`, `bad_request`, the timeouts), while one RAISED before the stream
  opened is written by the session runtime with `code` = the problem type (`/errors/provider-server-error`, `/errors/rate-limited`...).
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

- [workspaces](workspaces.md) - sessions live inside workspaces;
  workspaces own the filesystem + git state.
- [agents](agents.md) - the agent defines what a session does;
  the session is one instance of running an agent.
- [yielding](yielding.md) - yields are how sessions transition
  to WAITING; covers the `ask_user/pending` + `ask_user/respond`
  resume endpoints in depth.
- [tool-approval](tool-approval.md) - the approval-gate park and its
  own `tool_approval/pending` + `tool_approval/respond` endpoints.
- [graphs](graphs.md) - a graph session can park mid-run on an
  `ask_user` node and resume over the same endpoints.
  same yield mechanics, different lifecycle.
