# External tools

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

## Opt-in flag

`Agent.allow_external_tools` (`primer/model/agent.py`, default false)
gates the feature per agent. Invocation bodies carrying `external_tools`
against a flag-off agent are rejected with 422. The flag is mirrored into
the on-disk `AgentBinding` snapshot at session start
(`primer/workspace/session_factory.py`), and the runtime injection gate
reads the snapshot-first resolved agent, so the API gate and the worker
gate agree. Graph sessions accept defs when at least one agent node's
agent has the flag; injection then happens per node, so flag-off nodes
never see the defs.

## Entities (`primer/model/external_tool.py`)

`ExternalToolDef` is wire-only, never stored as its own entity: `name`
(pattern `^[a-z][a-z0-9_]{0,63}$`, no `__`), `description`, `args_schema`
(JSON wire alias `"schema"`, validated as a JSON Schema), optional
`timeout_seconds`. `validate_external_tool_defs` enforces the message
caps: at most 64 tools, at most 256 KiB serialized, no duplicate names.

`ExternalToolCall` (id prefix `etool`) is the stored record of one call:
owner (`session_id` or `chat_id`), `node_id` for graph attribution when
known, `tool_call_id`, `tool_name`, `arguments`, `status`
(`pending | completed | cancelled | timed_out`), `result` + `is_error`,
and the three timestamps. The park slot (or the chat pending slot)
remains the execution source of truth; these rows are the API-facing
discovery + audit surface and are kept in lockstep by the endpoints and
lifecycle sweeps. `ExternalToolResultIn` (`{tool_call_id, result,
is_error}`) is the shared result wire shape for both surfaces.

## Registration is per message

`external_tools` rides every invocation body: `SessionCreateBody` (for
the initial turn), `SteerBody`, `ChatSendMessageBody`, and the chat WS
`user_message` frame. The set on the turn-triggering message is the set
for that whole turn, stored on the owning row
(`WorkspaceSession.external_tools` / `Chat.external_tools`) as
`ExternalToolDef` dumps. The next turn-triggering message replaces the
set; a pure `tool_results` body leaves it untouched. Turns not triggered
by an invoker (trigger deliveries, scheduled wakes) never carry external
tools; graph-internal node turns inherit the graph session's defs.

## Turn mechanics (`primer/agent/external_tools.py`)

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

## The dispatch rule: one invocation endpoint

There is no separate respond API. `SteerBody` grew `tool_results`
alongside the now-optional `instruction`; `ChatSendMessageBody` and the
WS frame carry the same field. Every invocation applies, in order:

1. Validate `tool_results` against the pending calls. Any unknown or
   already-resolved id rejects the whole request (409) before any state
   changes.
2. Apply matching results: rows flip to `completed`, each park wakes
   durably (`durably_wake_session` for sessions; chats stamp
   `pending_tool_call.external_result` and flip claimable).
3. Message content cancels every still-pending external call with the
   synthetic result `{"cancelled": true, "reason": "superseded by new
   user message"}` (the park wakes with the cancelled marker payload so
   the turn pairs the call before consuming the message), then the
   message flows through the normal steer / chat-turn path.
4. A pure-results body just resumes; nothing else happens.

Session-side helpers live in `primer/session/external_tools.py`
(`apply_tool_results` is 409-atomic; `cancel_pending_external` flips
rows); the chat twin is `_apply_external_dispatch` in
`primer/api/routers/chats.py`.

## Read surface (`primer/api/routers/external_tools.py`)

`GET /v1/sessions/{id}/external_tools/pending` and
`GET /v1/chats/{id}/external_tools/pending` list one conversation's
pending calls (`tool_call_id`, `tool_name`, `arguments`, timestamps,
`node_id`). `GET /v1/external_tool_calls` is the global paged list
(filters: `status`, `session_id`, `chat_id`): the cross-conversation
poll point for orchestrators and the audit trail of resolved rows.

Timeout is materialised lazily on read: worker resume hooks have no
storage handle, so `sweep_expired` flips any pending row whose
`timeout_at` passed to `timed_out` wherever rows are read, while the
park itself resumes through the existing `parked_until` sweeper.

## Chat surface

Chats never park. The runner's `soft_yield`
(`primer/chat/executor.py`) handles `_external` as a third pending mode
beside ask_user and approval: it records
`Chat.pending_tool_call = {mode: "external", name, arguments,
external_call_row_id, ...}` and appends a persisted
`external_tool_call` message row, which is the WS push frame: it flows
to connected clients live AND replays on reconnect. The dispatch loop
(`primer/chat/dispatch.py`) resumes an external pending only when the
invocation endpoint has stamped `external_result` onto the pending dict;
`resume_external_pending` then appends the paired `tool_result` row and
the turn continues. Cancel-while-awaiting and rewind reuse
`abandon_pending_rows` / `flip_external_row` (`primer/chat/pending.py`).

## Graph sessions

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

## Lifecycle

Session cancel / force-delete / restart, the yield-cancel endpoint
(`POST /v1/sessions/{sid}/yields/{tcid}/cancel`), chat end/delete, chat
rewind, and chat cancel-while-awaiting all resolve open rows to
`cancelled` so the audit surface never dangles.

## Shell surface

The console shows a pending banner (`ui/components/external-tools.jsx`,
`window.ExternalPendingBanner`) on the shell's session document
(`ui/components/shell/sh-session-doc.jsx`): read-only plus operator
cancel; responding is the invoker's job. The agent editor
(`ui/components/agents.jsx`) exposes the `allow_external_tools` toggle.
Scripting goes through the REST surface directly: list and pending are
reads, and responding posts a `tool_results` body to the invocation
endpoint, which resolves the owning workspace itself.

## Tests

`tests/model/test_external_tool.py` (validation matrix),
`tests/agent/test_external_tools.py` (provider + manager + resume hook),
`tests/api/test_external_tools_steer.py` (dispatch rule),
`tests/api/test_external_tools_read.py` (read surface + lazy timeout),
`tests/api/test_external_tools_chat_api.py` + 
`tests/chat/test_external_tools_chat.py` (chat surface),
`tests/api/test_external_tools_lifecycle.py`,
`tests/api/test_external_tools_graph.py` + 
`tests/api/test_external_tools_graph_create.py` (graphs),
`tests/ui/test_external_tools_ui.py`,
and the fake-LLM
round-trip in `tests/worker/test_external_tools_roundtrip.py`.

> **Historical decisions.** The design spec called for a chat Part-union
> extension and a transient WS push frame; the implementation uses a
> uniform `tool_results` body field on both surfaces and a persisted
> `external_tool_call` message row (reconnect-replayable) instead. The
> spec's separate respond endpoint was dropped during brainstorming in
> favour of the unified invocation API. Rows for graph agent-node calls
> carry `node_id` only via the checkpoint (the per-node resolver does
> not thread node ids); the pending endpoints surface `node_id` when the
> row has it.
