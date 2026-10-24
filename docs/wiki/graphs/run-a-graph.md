---
title: "Run a graph"
summary: "Run a graph in a session or as a step of another session, pass it input, answer its questions and read its output."
order: 3
tools: [workspace_ext__invoke_graph]
related: [sessions/start-a-session, human-in-the-loop/ask-user, graphs/threads]
---
## What it is
A graph runs inside a workspace session, in one of two ways. Start a session bound to the graph, with its input in `graph_input`. Or, from inside a running session, call `workspace_ext__invoke_graph` to run the graph as one step of your own turn and get its output back.

## Concepts
- A graph session is a session whose binding is `{"kind": "graph", "graph_id": ...}`. It is autonomous: it ends when the run ends, `ended_reason` `completed` or `failed`. It does not rest waiting for more input the way an agent session does.
- `graph_input` is the run's input, exactly as you pass it: an object, a string or a list of messages. Templates read it as `initial_input`. When the `begin` node has an `input_schema`, the session create checks `graph_input` against it and refuses a mismatch, or a missing input, with a `validation-error`.
- `initial_instructions` are not the graph's input. They are only read, as JSON, when the `begin` node has an `input_schema` and no `graph_input` was sent.
- The output is the `end` node's rendered `output_template`. In the transcript it is the last `assistant_token` record; a failure is an `error` record with `code`, `message` and `node_id`. Records carry the `node_id` of the node that wrote them.
- `workspace_ext__invoke_graph` takes a `graph_id` and an `input` string. The child graph runs in your session's workspace, nested under your session, and your turn waits for it. It answers `{"output": "<text>"}`. A failed child is an error result, never an empty output: `{"rejected": true, "reason": ..., "tool_name": ...}` when an approval inside it was refused, timed out or was cancelled, else `{"error": "<code>", "message": ..., "node_id": ...}`.
- A node that needs a person parks the whole run: a `tool_call` node on `system__ask_user`, an agent node that asks, or an approval gate. The session shows `status` `waiting` (`session_state` `parked`). When several nodes park in one step, each question is answered on its own, and the run moves on when all are answered.
- Only a person answers: in the console, or over the channel the workspace is bound to ([reply binding](channels/reply-binding)). An agent cannot answer for the user. The person must rank at or above the session's creator, or own the session; an admin always can.
- A question that times out or is cancelled reaches the node as `{"timed_out": true, ...}` or `{"cancelled": true, ...}`, never as an empty answer. An approval that is refused, times out or is cancelled on a `tool_call` node fails the run (`tool_approval_rejected`, `tool_approval_timeout`, `tool_approval_cancelled`). On an agent node, the model reads the refusal and goes on.
- An `ask_user` node's output text is `{"response": ...}`. To route on the answer, give the node an `output_schema` for that object and read `response` in the router.

## How to
1. Check the graph's input contract: read the `begin` node's `input_schema` and the templates that read `initial_input`.
2. Start a graph session in your workspace, with `graph_input` in the shape the templates read.
3. Poll the session with `workspaces__get_workspace_session` until `status` is `ended`. While it is `waiting`, a person has a question or an approval to settle.
4. Read the transcript: the last `assistant_token` is the output; an `error` record says which node failed and why.
5. From inside a session, to use a graph as one step instead, call `workspace_ext__invoke_graph` and read `output` from the result.

## Tool calls
### Start a graph session
`workspaces__create_workspace_session` binds a new session to the graph and starts it with its input.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "ws-pipelines", "binding": {"kind": "graph", "graph_id": "extract-then-summarise"}, "graph_input": {"text": "Acme opened an office in Berlin."}}}
```

Reference: [`workspaces__create_workspace_session`](reference/tools/workspaces/create_workspace_session)

### Read the output
`workspaces__read_workspace_session_messages` returns the transcript; pass `after_seq` to read only what is new.

```json
{"toolset_id": "workspaces", "tool_name": "read_workspace_session_messages", "arguments": {"workspace_id": "ws-pipelines", "session_id": "sess-3f9a1c8d2b7e", "tail": true, "limit": 20}}
```

Reference: [`workspaces__read_workspace_session_messages`](reference/tools/workspaces/read_workspace_session_messages)

### Invoke a graph from a session
`workspace_ext__invoke_graph` runs the graph as a step of your turn and returns its output text.

```json
{"toolset_id": "workspace_ext", "tool_name": "invoke_graph", "arguments": {"graph_id": "summarise-ticket", "input": "Ticket 42: the login page times out after the last deploy."}}
```

Reference: [`workspace_ext__invoke_graph`](reference/tools/workspace_ext/invoke_graph)

## Gotchas
- `invoke_graph` sends its `input` as one user message, so the child's `initial_input` is a list of messages. The `begin` node's `input_schema` is not checked, and a template that reads `initial_input.text` fails. Invoke graphs whose templates use the default `input_template` or `nodes.<begin id>.text`.
- `invoke_graph` works only inside a workspace session: not over MCP, and not from a caller with no session. Invocations nest at most 8 deep by default; past that the call is refused.
- A session outside the default workspace can start sessions only in its own workspace. Another workspace answers `not-found`, as an unknown id does.
- A message to a graph session (`workspaces__message_session`) restarts the graph once the current run is over, or reopens it when it ended. The run takes its input from the stored `graph_input` only: the message is recorded in the transcript, but it is not the graph's input. With no stored `graph_input`, the input is an empty list. See [messaging other sessions](sessions/messaging-other-sessions).
- A Stop on a graph session takes effect only between graph events, not in the middle of a node. To end the run, cancel the session.
- A graph session reads the stored graph when its run starts, and each `graph` node reads its child when it runs. Editing a graph mid-run reaches the next run, not the current step.
- Graph runs keep their state in the workspace, under the session. They do not write [graph threads](graphs/threads).

## Related
[start a session](sessions/start-a-session), [ask the user](human-in-the-loop/ask-user), [graph threads](graphs/threads).
