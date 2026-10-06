# External tools

## 1. Purpose

Invoker-supplied tool calls: an API caller invoking an agent attaches its
own tool definitions to the invocation, the model calls them like any
other tool, and the conversation pauses until the caller supplies the
result through the same invocation API. This is the platform's client-side
tool-use loop, the same shape as Anthropic's Messages API pattern: the
tool "executes" wherever the caller runs, not on the server.

Three consumers drive the design: host applications that lend Primer
agents their own capabilities, human-in-the-loop frontends whose "tool
result" is a person's input, and external orchestrators that drive Primer
agents as workers.

## 2. Conceptual model

The tool executes wherever the CALLER runs. The server's job is to hold
the conversation open while that happens and to accept the result through
the same endpoint that started the invocation. Everything else follows:
the call is a durable row rather than a transient frame, because the
caller may take minutes and may reconnect; and there is exactly one
endpoint, because a separate respond route would let a caller respond to
an invocation it never made.

## 3. Architecture patterns implemented

- **One invocation endpoint, both directions.** The result arrives on the
  same route as the request, as a `tool_results` body field.
- **The pending call is a persisted row, not a push frame.** It flows to
  connected clients live AND replays on reconnect.
- **Timeout is materialised lazily on read.** Worker resume hooks have no
  storage handle, so a pending row whose deadline passed flips wherever
  rows are read.
- **The flag is per agent, opt-in.** An agent that never expects
  invoker-supplied tools cannot be handed them.

## 4. Code layout

`primer/model/external_tool.py` holds the entities;
`primer/agent/external_tools.py` owns the turn mechanics and the resume
hook; `primer/session/external_tools.py` applies results and cancels
calls for the invocation endpoints; `primer/session/external_calls.py`
holds `resolve_external_row`, the one write of a row's status;
`primer/api/routers/external_tools.py` is the read surface. The
console banner is `ui/components/external-tools.jsx`.

## 5. Data model

### Opt-in flag

`Agent.allow_external_tools` (`primer/model/agent.py`, default false)
gates the feature per agent. Invocation bodies carrying `external_tools`
against a flag-off agent are rejected with 422. The flag is mirrored into
the on-disk `AgentBinding` snapshot at session start
(`primer/workspace/session_factory.py`), and the runtime injection gate
reads the snapshot-first resolved agent, so the API gate and the worker
gate agree. Graph sessions accept defs when at least one agent node's
agent has the flag; injection then happens per node, so flag-off nodes
never see the defs.

### Entities (`primer/model/external_tool.py`)

`ExternalToolDef` is wire-only, never stored as its own entity: `name`
(pattern `^[a-z][a-z0-9_]{0,63}$`, no `__`), `description`, `args_schema`
(JSON wire alias `"schema"`, validated as a JSON Schema), optional
`timeout_seconds`. `validate_external_tool_defs` enforces the message
caps: at most 64 tools, at most 256 KiB serialized, no duplicate names.

`ExternalToolCall` (id prefix `etool`) is the stored record of one call:
its owning `session_id`, `node_id` for graph attribution when known,
`tool_call_id`, `tool_name`, `arguments`, `status`
(`pending | completed | cancelled | timed_out`), `result` + `is_error`,
and the three timestamps. The park slot remains the execution source of
truth; these rows are the API-facing discovery and audit surface, kept in
lockstep by the endpoints and the lifecycle sweeps. A row is created
`pending` and leaves `pending` once: every status write is
`resolve_external_row` (see section 9), so the first terminal status it
reaches is the one it keeps.
`ExternalToolResultIn` (`{tool_call_id, result, is_error}`) is the result
wire shape.

### Registration is per message

`external_tools` rides every invocation body: `SessionCreateBody` (for
the initial turn) and `SteerBody`. The set on the turn-triggering message
is the set for that whole turn, stored on the owning row
(`WorkspaceSession.external_tools`) as `ExternalToolDef` dumps. The next turn-triggering message replaces the
set; a pure `tool_results` body leaves it untouched. Turns not triggered
by an invoker (trigger deliveries, scheduled wakes) never carry external
tools; graph-internal node turns inherit the graph session's defs.

## 6. Lifecycle

### Turn mechanics (`primer/agent/external_tools.py`)

`ExternalToolsetProvider` materialises one invocation's defs as an
in-memory `ToolsetProvider` registered under the reserved toolset id
`external` (the id is rejected for stored toolsets by the reserved-id
guard in `primer/api/routers/providers.py`). The
`ToolExecutionManager` merges its tools into the catalogue as
`external__<name>`, bypassing the agent allowlist (they are
per-invocation grants, not `Agent.tools` entries) and skipping the
approval gate (the caller mediates every call by construction).

Dispatching one writes the pending `ExternalToolCall` row, then raises
the same `YieldToWorker` the ask_user tool uses, with the park marker
tool name `_external` (mirroring `_approval`: the real name is dynamic
and cannot key the resume registry, so the marker does, with
`original_call` and `external_call_row_id` in `resume_metadata`). The
event key is `external_tool:{owner_id}:{tool_call_id}`.

The `_external` resume hook (registered at import; the worker's
`session_resume_coordinator` imports the module explicitly so a process
that never injected external tools can still resume one) translates the
wake payload into the tool result: `{"result", "is_error"}` verbatim
from the invoker, `{"timed_out": true}` for a `YieldTimeout`, and
`{"cancelled": true, "reason": ...}` for a `YieldCancelled`.

## 7. Persistence

A pending call is a storage row for as long as the caller has not
answered, which is what lets a client disconnect and pick the
conversation back up. Nothing else about an external call is durable: the
result becomes an ordinary tool-result record on the session's own
transcript.

## 8. Public surfaces

### The dispatch rule: one invocation endpoint

There is no separate respond API. `SteerBody` grew `tool_results`
alongside the now-optional `instruction`; `ChatSendMessageBody` and the
WS frame carry the same field. Every invocation applies, in order:

1. Validate `tool_results` against the pending calls. Any unknown or
   already-resolved id rejects the whole request (409) before any state
   changes.
2. Apply matching results: each park wakes durably through
   `durably_wake_session`, then its row flips to `completed` (a row that
   left `pending` in between, because a cancel or the lazy timeout won,
   keeps that status).
3. Message content cancels every still-pending external call with the
   synthetic result `{"cancelled": true, "reason": "superseded by new
   user message"}` (the park wakes with the cancelled marker payload so
   the turn pairs the call before consuming the message), then the
   message flows through the normal steer path.
4. A pure-results body just resumes; nothing else happens.

The helpers live in `primer/session/external_tools.py`:
`apply_tool_results` is 409-atomic and `cancel_pending_external` flips
rows. `cancel_pending_external` cancels every `pending` row of the owner,
or only the rows named by `tool_call_ids`, or only the rows created
strictly before `created_before` (an aware datetime compared in Python;
a row with no `created_at` is then spared), and returns the
`tool_call_id`s whose `cancelled` write landed. A row that was resolved
meanwhile is not in that list. The steer wakes the parks with the
cancelled marker only when that list is not empty.

### Read surface (`primer/api/routers/external_tools.py`)

`GET /v1/sessions/{id}/external_tools/pending` lists one session's
pending calls (`tool_call_id`, `tool_name`, `arguments`, timestamps,
`node_id`). `GET /v1/external_tool_calls` is the global paged list
(filters: `status`, `session_id`): the cross-session poll point for
orchestrators and the audit trail of resolved rows.

Timeout is materialised lazily on read: worker resume hooks have no
storage handle, so `sweep_expired` flips any pending row whose
`timeout_at` passed to `timed_out` wherever rows are read, while the
park itself resumes through the existing `parked_until` sweeper. The
flip is the guarded write, so a result that landed after the list was
read keeps its `completed`; the sweep then refreshes the rows it read in
place (from the written row, or from one fresh read when the write was
rejected), so both lists report the row's real status rather than the
`pending` they read.

### Graph sessions

The per-node tool manager resolver (`primer/worker/executor_builders.py`)
injects the graph session's defs into each agent node's manager, gated
by that node agent's flag. An agent-node call parks through the graph
checkpoint's `pending_agent_yields`; a value-yielding tool-call node
would ride `pending_toolcalls`. Both are answerable individually over
the steer endpoint: `_pending_targets` reads the `_external` entries'
wake keys, and multi-event parks accumulate per-call resume payloads.
The graph resume coordinator's generic value-yield path (anything with a
registered resume hook, `primer/graph/_node_refs.py`) synthesises the
node result from the payload, so no graph-specific external code exists
on the resume side. Message content cancels ALL pending external calls
across nodes, session-wide.

### Lifecycle notes

Session cancel, delete and restart, a Stop that ends or cancels an
external call (`primer/session/dispatch.py`), and the yield-cancel
endpoint (`POST /v1/sessions/{sid}/yields/{tcid}/cancel`) all resolve
open rows to `cancelled` so the audit surface never dangles (rewind does
not touch the rows). Each of them cancels only a row that is still
`pending`; a row a result already completed stays `completed`. The
yield-cancel endpoint's row write is best effort: a row that already
left `pending` or no longer exists is skipped silently, any other
storage error is logged, and the endpoint still answers 202.

### Shell surface

The console shows a pending banner (`ui/components/external-tools.jsx`,
`window.ExternalPendingBanner`) on the shell's session document
(`ui/components/shell/sh-session-doc.jsx`): read-only plus operator
cancel; responding is the invoker's job. The agent editor
(`ui/components/agents.jsx`) exposes the `allow_external_tools` toggle.
Scripting goes through the REST surface directly: list and pending are
reads, and responding posts a `tool_results` body to the invocation
endpoint, which resolves the owning workspace itself.

## 9. Internal contracts

- **The result arrives on the invocation endpoint, never a separate
  respond route.** A separate route would let a caller answer an
  invocation it did not make.
- **A pending call replays on reconnect.** It is a persisted row, so a
  client that drops mid-call does not lose the question.
- **A pending call whose deadline passed is `timed_out` wherever it is
  read.** There is no sweeper for it, and there does not need to be.
- **A row's status is written by ONE guarded write, and the first
  terminal status wins.** All four writers (`apply_tool_results`:
  `completed`; `cancel_pending_external`: `cancelled`; `sweep_expired`:
  `timed_out`; `flip_external_row`, used by the yield-cancel endpoint:
  `cancelled`) call `resolve_external_row(storage, row_id, *, status,
  result, is_error)`, one `patch_if` of `status`, `result`, `is_error`
  and `resolved_at` guarded on `status == "pending"`. It returns the
  written row, `None` when the guard rejected the write (the row already
  left `pending`), and raises on a storage error (`NotFoundError` for a
  missing row). Nothing writes the whole row from a snapshot, so a
  writer that read the row `pending` and lost a race cannot overwrite
  the winner. `flip_external_row` keeps its best-effort contract around
  it. A new writer of a row's status must go through the helper. The
  patch is encoded with `to_jsonable_python(..., inf_nan_mode="null")`,
  the way a whole-row write dumps the model, so a NaN or infinity in a
  result is stored as `null`, exactly what the park receives; a value no
  JSON holds (an arbitrary object, bytes that are not UTF-8, a lone
  surrogate) raises before the row is written.
- **Any of the row's writers can win, and the row can disagree with the
  park.** The writers that can reach a `pending` row first are the lazy
  timeout (`sweep_expired`, on every GET list), the yield-cancel endpoint
  (`flip_external_row`), `cancel_pending_external` from the steer's
  instruction, session cancel, delete and restart and the Stop cleanup,
  and the result path (`apply_tool_results`). The result path wakes the
  park BEFORE it writes the row, so a cancel or a timeout that lands
  between the two leaves the row `cancelled` or `timed_out` while the
  park (and so the model) took the result and the steer answered 2xx.
  The park is the execution truth; the row is the audit record and is
  not reconciled with it yet. (Before the guarded write, the result's
  whole-row write turned such a row `completed`, and in the opposite
  order a late cancel or timeout overwrote a `completed` row.)
- **`created_before` must be timezone-aware.** `cancel_pending_external`
  compares it with each row's aware `created_at` in Python; a naive value
  raises `ValueError` before anything is read.
- **`node_id` is surfaced only when the row carries it.** Graph
  agent-node calls carry it via the checkpoint; the per-node resolver
  does not thread node ids, so the endpoints report what is there rather
  than inventing it.

## 10. Testing patterns

`tests/model/test_external_tool.py` (validation matrix),
`tests/agent/test_external_tools.py` (provider + manager + resume hook),
`tests/api/test_external_tools_steer.py` (dispatch rule),
`tests/api/test_external_tools_lifecycle.py` (lifecycle),
`tests/session/test_external_row_guarded_writes.py` (the guarded write,
the cancel filters and their landed ids, and the result and cancel
races) and `tests/api/test_external_tools_guarded_writes.py` (the lazy
timeout, the yield-cancel and sweep races; both race files run on real
SQLite and hold the losing writer AT its write with
`tests/_support/held_write.py`),
`tests/api/test_external_tools_graph.py` + 
`tests/api/test_external_tools_graph_create.py` (graphs),
`tests/ui/test_external_tools_ui.py`,
and the fake-LLM
round-trip in `tests/worker/test_external_tools_roundtrip.py`.

## 11. Historical decisions

- **A uniform `tool_results` body field, not a chat Part-union
  extension.** Why: the design spec proposed a per-surface extension and
  a transient WS push frame. One body field on the invocation endpoint
  plus a persisted, reconnect-replayable row said the same thing on every
  surface, and outlived the surface it was first written for: the chat
  engine it originally targeted no longer exists, and external tools
  needed no change when it went.
- **The spec's separate respond endpoint was dropped.** Why: the unified
  invocation API already identifies the caller and the call.
