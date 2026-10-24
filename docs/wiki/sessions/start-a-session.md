---
title: "Start a session"
summary: "Start an agent or graph in a workspace with a brief, and decide whether it runs at once."
order: 2
tools: [workspaces__create_workspace_session]
related: [sessions/observe-a-session, graphs/run-a-graph, workspaces/create-and-delete]
---
## What it is
`workspaces__create_workspace_session` starts a session: one agent or graph working inside a workspace on the brief you give it. The answer is the new session row; keep its `id`, every other session tool needs it.

## Concepts
- **`workspace_id`** names the workspace the session works in. From a session outside the default workspace (prime) you may start sessions only in your own workspace; another one answers `not-found`, as an unknown id does.
- **`binding`** says what runs: `{"kind": "agent", "agent_id": "..."}` or `{"kind": "graph", "graph_id": "..."}`. Either kind takes an optional `profile_id`, a model profile used for this session instead of the agent's own.
- **The brief.** An agent gets `initial_instructions`, its first user message. A graph gets `graph_input`, any JSON; when the graph's begin node has an input schema, `graph_input` is required and is checked against it before the session is created.
- **`auto_start`** (default `true`) arms the first turn at once, and the answer reads `status` `running`. With `false` the session is created `created` and stays inert: nothing runs it until a message reaches it (`workspaces__steer_workspace_session`) or a person starts it from the console.
- **`autonomous`** decides how the session ends. Leave it out for an agent session you will keep talking to: after each clean turn it rests (`session_state` `parked`) and waits for the next message. Set `autonomous: true` when the session should end when its agent finishes, so `session.ended` fires and a poll for `ended` works. A graph session ends after its run without it.
- **`parent_session_id`** records lineage only. Nothing propagates between parent and child: a cancel of the parent does not reach the child.
- **`metadata`** is a free-form object stored on the row.
- **Rank.** A session started from your session runs at your run's rank (the principal that started your run). A session started over MCP with no calling session is recorded as unattributed and runs as an ordinary user.
- The tool does not wait for the session. It returns as soon as the row exists and the first turn is armed.

## How to
1. Pick the workspace. Use one workspace per task, so the sessions of one task share files and nothing else.
2. Make sure the agent or graph exists (`system__get_agent`, `system__get_graph`); a missing one is refused.
3. Write the brief: the goal, the inputs, where to put the result, and when to stop. The session sees only what you write here and what is in the workspace.
4. Decide `autonomous`: `true` for a one-shot job you will wait on, left out for a session you will keep steering.
5. Call `workspaces__create_workspace_session` and keep the returned `id`.
6. Wait for the result. Inside a session, wait for `session.ended` with `system__wait_for_event` (below, and [waiting for events](triggers/waiting-for-events)); this works only for an autonomous session. Outside a session, poll `workspaces__get_workspace_session` until `status` is `ended`. For an interactive session, poll the transcript instead ([observe a session](sessions/observe-a-session)).
7. Read the result from the transcript or from the file the brief named.

## Tool calls
### Start an agent session that ends when it is done
Start the agent on a one-shot brief; `autonomous: true` makes the session end after its turn.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "ws-docs", "binding": {"kind": "agent", "agent_id": "code-reviewer"}, "initial_instructions": "Review src/parser.py for error handling. Write your findings to reports/parser-review.md, then stop.", "autonomous": true}}
```

Reference: [`workspaces__create_workspace_session`](reference/tools/workspaces/create_workspace_session)

### Start a graph session
Run a graph on a structured input; the graph session ends when the run ends.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "ws-docs", "binding": {"kind": "graph", "graph_id": "incident-pipeline"}, "graph_input": {"ticket": "INC-1042"}}}
```

### Create a session without running it
`auto_start: false` creates the session and leaves it inert until a message reaches it.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "ws-docs", "binding": {"kind": "agent", "agent_id": "code-reviewer"}, "initial_instructions": "Wait for the go-ahead before you start.", "auto_start": false}}
```

### Wait for the session to end
From inside your own session, park your turn until the child ends. Use the `id` the create call returned.

```json
{"toolset_id": "system", "tool_name": "wait_for_event", "arguments": {"event_types": ["session.ended"], "fields": [{"path": "session_id", "op": "eq", "value": "sess-3f9a1c8d2b7e"}], "timeout_seconds": 1800}}
```

## Gotchas
- An interactive agent session never ends by itself. A wait for `session.ended` on it waits until the timeout; start it with `autonomous: true` or watch its transcript.
- A missing agent or graph is refused with `validation-error`, and nothing is created.
- A graph that cannot run is refused with every problem listed in one message: no nodes, not exactly one begin node, no end node, an end node the begin node cannot reach, or a loop without `max_iterations`.
- A `graph_input` that does not match the begin node's schema is refused with the path of the first mismatch.
- A workspace this deployment refuses (a workspace on a local provider where the workers are remote) is refused with an error that says so; a workspace being deleted answers `not-found`.
- A session tool from another workspace's session cannot see this session: keep the work in one workspace, or drive it from prime.

## Related
[observe a session](sessions/observe-a-session), [control a session](sessions/control-a-session), [run a graph](graphs/run-a-graph), [create and delete a workspace](workspaces/create-and-delete), [waiting for events](triggers/waiting-for-events).
