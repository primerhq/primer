---
slug: yielding-tools
title: Yielding tools
section: features
summary: Tools that suspend a session and release its worker while waiting, and how the session parks and resumes when the event arrives.
---

## What yielding is

Some tool calls are not instant computations. They are waits:

- `ask_user` waits for a person to type a reply.
- `subscribe_to_trigger` waits for a scheduled time or an incoming event.
- `misc__sleep` waits for a duration.
- `watch_files` waits for a filesystem change.
- A tool approval gate waits for an operator decision.

If each waiting tool occupied a worker for its entire duration, a modest number of concurrent long-running waits would exhaust the worker pool. A system with ten workers and eleven sessions all waiting on user replies would leave the eleventh session unable to run any new work at all.

Yielding breaks that coupling. When a tool yields, it does not hold a worker while it waits. The session parks in durable storage, the worker is freed to handle other sessions, and the run resumes later when the awaited event fires.

```mermaid
stateDiagram-v2
    [*] --> Running : worker claims the session
    Running --> Parked : yielding tool returns sentinel\n(lease released)
    Parked --> Resumable : awaited event fires
    Resumable --> Running : worker claims lease and\nresumes from park point
    Running --> Ended : run completes (lease dropped)
    Parked --> Cancelled : cancel requested
    Cancelled --> [*]
    Ended --> [*]
```

The key insight is the gap between Parked and Resumable: during that window the run exists only in storage. No worker slot is consumed, no network connection is held. A parked session survives an arbitrary wait (seconds or hours) without resource cost.

## How a session parks and resumes

When a tool yields, it returns a sentinel value instead of a result. The runtime detects the sentinel and does three things atomically:

1. Writes the paused state (the in-progress message history, the tool name, and the event key the tool is waiting on) into durable storage.
2. Releases the worker lease, making the worker immediately available to claim other work.
3. Marks the session as parked.

Each yielding tool registers an event key when it parks. Common keys look like:

| Event key pattern | Yielding tool |
|---|---|
| `ask_user:{scope_id}:{call_id}` | `misc__ask_user` |
| `trigger:{trigger_id}` | `trigger__subscribe_to_trigger` |
| `tool_approval:{session_or_chat_id}:{call_id}` | Tool approval gate |
| `timer:{call_id}` | `misc__sleep` |
| `watch:{session_id}:{call_id}` | `workspaces__watch_files` |

When the awaited event fires (the user replies, the trigger ticks, the operator approves, the file changes), the platform publishes a message on that event key. The event listener picks it up, finds the matching parked row, and marks it resumable. From that moment the session is eligible to be claimed by any available worker.

When a worker claims the resumed session, it rehydrates the `ParkedState` blob (the LLM message history, the pending tool call id, and the yield resume metadata) and continues the turn from exactly where it stopped. To the LLM the park is invisible: the resumed turn sees the tool result it was waiting for and continues.

## Which tools yield

### misc__ask_user

Asks the user (or the channel the session is bound to) a question and parks until a reply arrives. The reply is injected as the tool result. If no reply arrives within the configured timeout, the tool result indicates a timeout. If the operator cancels the yield via the console, the result indicates cancellation.

Use `ask_user` when the agent needs a human decision before it can continue. Unlike `inform_user`, which sends a one-way message without parking, `ask_user` suspends the session entirely until a reply is received.

### misc__sleep

Parks the session for a fixed number of seconds (0 to 300). A background sweeper publishes the timer event when the duration elapses. Zero-second sleeps short-circuit without parking. Fractional values are accepted.

Use `sleep` for polling loops, rate-limit backoffs, or any pattern where an agent must wait a known duration before retrying.

### trigger__subscribe_to_trigger

Parks the session until a named trigger fires, then resumes with the fire context (trigger id, slug, kind, fired-at timestamp, and a deterministic fire id) as the tool result. The `parked_session` subscription that wakes the session is written before the park takes effect, so a trigger fire that races the park still finds the subscription and wakes the session correctly.

Use `subscribe_to_trigger` with a scheduled trigger to implement recurring agent work: the agent parks after each run, the trigger wakes it on the next cron tick, and the cycle repeats without a held connection.

```mermaid
sequenceDiagram
    participant Agent
    participant Tool as subscribe_to_trigger
    participant Session
    participant Trigger
    Agent->>Tool: call(trigger_id)
    Tool->>Session: park on trigger event key
    Session-->>Agent: (parked - no response yet)
    Note over Trigger: trigger fire time arrives
    Trigger->>Session: wake via parked_session subscription
    Session->>Agent: resume with fire context as tool result
```

### workspaces__watch_files

Parks the session until one or more workspace-relative paths change on disk. A background watcher polls file modification times and publishes a coalesced change burst on the event bus. On resume, the tool result carries a `changes` list where each entry includes the `path`, `event_type` (created / modified / deleted), and `mtime_after`.

Parameters:

| Parameter | Default | Description |
|---|---|---|
| `paths` | required | Workspace-relative paths to watch (files or directories; directories watch child files one level deep). Absolute paths and `..` segments are rejected. |
| `timeout_seconds` | none | Optional timeout; falls back to the global yield cap. On timeout the result is `{timed_out: true, changes: []}`. |
| `batch_window_ms` | 250 | After the first change is detected, the watcher waits this many milliseconds for more changes before publishing one coalesced burst. Increase for noisy file systems. |

Use `watch_files` when an agent must block until an external process writes or modifies a file in the workspace, for example waiting for a build output, a generated report, or another agent's write.

### workspaces__invoke_graph

Runs a named graph inside the current workspace session and returns its output text. The invoked graph's state nests under the calling session. This tool is classified as yielding because graph runs that involve human-in-the-loop steps (ask_user, tool approval gates) park the calling session while those gates are open.

Use `invoke_graph` when you need to delegate a self-contained multi-step workflow to a graph from within a session.

### system__switch_to_agent (chat-only)

Hands the current chat off to a different agent. The switch parks the current turn and the chat resumes with the new agent. This tool is only available in chat sessions, not in workspace sessions.

### Tool approval gates

When a tool approval policy is configured for a tool and the policy verdict requires approval, the platform raises a yield internally. The session parks with the event key `tool_approval:{scope_id}:{call_id}` and transitions to `WAITING` with a `_ToolApprovalWaiting` marker. An operator approves or rejects the call in the console; on approval the turn re-executes the tool call bypassing the gate, on rejection the tool result is an error.

Tool approval gates are not themselves a named tool; they are a yield raised by the tool dispatch layer whenever an active policy gates a call.

```ref:features/sessions
The session lifecycle walkthrough, including how parked sessions appear in the console and the pending ask_user endpoint.
```

```ref:features/triggers
Creating triggers and subscriptions, and how subscribe_to_trigger parks a session.
```

```ref:features/toolsets-system
The misc, system, trigger, and workspaces toolsets where yielding tools live.
```

```ref:features/workers
How the worker pool and claim engine pick up parked sessions when they become resumable.
```

## Configuration

Yielding tools do not require separate configuration. To make a tool available to an agent, add its toolset to the agent's tool list:

- `misc__ask_user`, `misc__sleep`: add the **misc** toolset or select individual tools.
- `trigger__subscribe_to_trigger`: add the **trigger** toolset.
- `workspaces__watch_files`, `workspaces__invoke_graph`: add the **workspaces** toolset; these tools are only available inside workspace sessions.
- Tool approval policies: configure under the **Approvals** page; the gate is active for any session that calls the matching tool.

```embed:approvals
```

## Walkthrough: using ask_user in a session

1. Open **Agents** and create or edit an agent. On the tools tab, add the **misc** toolset (or enable `ask_user` as an individual tool).
2. Open **Workspaces**, open a workspace, and start a new session bound to that agent.
3. Give the agent a task that it cannot complete without a human decision, for example: "Decide which of these two filenames you prefer and create the file."
4. Watch the session status. When the agent calls `ask_user`, the status changes to **Waiting**. The console shows the pending question.
5. Click the pending question in the session detail view and type a reply. Click **Send**.
6. The session status returns to **Running** as the agent receives the reply and continues its turn.

```embed:session-detail
```

```embed:sessions-list
```

## What happens after

Once a session parks, it consumes no worker slots. The parked row survives process restarts. Other sessions run unimpeded in the worker pool.

When the awaited event fires, the session becomes resumable and enters the normal claim queue. The next available worker picks it up and the agent continues from where it left off, with the tool result injected into its history. The agent sees no discontinuity; the park is transparent to the LLM.

A parked session can be cancelled from the console or the API at any time. A cancel while parked transitions the session directly to Ended/cancelled without resuming the turn.

If the global yield timeout elapses before the awaited event fires, the platform synthesises a timeout result (for example `{timed_out: true}` for `watch_files` and `sleep`) and resumes the session so the agent can handle the timeout gracefully.

```ref:features/workers
Worker pool capacity, the claim engine, and how parked sessions re-enter the queue.
```

```ref:reference/api-sessions
The API surface for the pending ask_user endpoint and the cancel-yield endpoint.
```
