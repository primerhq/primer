---
title: Create and run a graph
summary: Approve a runnable graph definition, execute it in a workspace and verify the end output before cleanup.
audience: admin
tools: [system__create_graph, system__update_graph, system__delete_graph, system__get_graph, system__find_graphs, system__list_graphs, system__create_graph_thread, system__get_graph_thread, system__get_agent, system__describe_tool, crud__create_graph, crud__update_graph, workspace_ext__invoke_graph, workspaces__get_workspace, workspaces__create_workspace_session, workspaces__get_workspace_session, workspaces__read_workspace_session_messages]
related: [graphs/create-and-edit, graphs/run-a-graph, graphs/threads, runbooks/create-an-agent, runbooks/manage-a-workspace]
---
## Goal

Create or edit a runnable graph, execute its nodes in a workspace, collect the end node's actual output and deliberately clean up only the disposable definition.

## Preconditions

- Run as a known authenticated user. Graph creation stamps the creator as owner; reads and runs need read, edits need full and deletion needs owner. The run's person must also be able to read node agents/subgraphs and use their profiles and toolsets.
- Verify the node agent `w7-agent` through [`system__get_agent`](reference/tools/system/get_agent). If absent or unusable, follow [create an agent](runbooks/create-an-agent) first. Keep it until this graph has been removed.

```json
{"toolset_id": "system", "tool_name": "get_agent", "arguments": {"id": "w7-agent"}}
```

- Verify an available workspace with full access using [`workspaces__get_workspace`](reference/tools/workspaces/get_workspace). The example uses `ws-example` only after this read proves it exists; otherwise follow [manage a workspace](runbooks/manage-a-workspace) and carry that returned id forward. No graph-specific run endpoint is required.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace", "arguments": {"id": "ws-example"}}
```

- Use [`system__find_graphs`](reference/tools/system/find_graphs) to check `w7-graph` before creation, or [`system__list_graphs`](reference/tools/system/list_graphs) to browse visible definitions. Follow pagination when needed; a duplicate id is not a reason to overwrite someone else's graph.

```json
{"toolset_id": "system", "tool_name": "find_graphs", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "id"}, "op": "=", "right": {"kind": "value", "value": "w7-graph"}}, "limit": 20}}
```

```json
{"toolset_id": "system", "tool_name": "list_graphs", "arguments": {"limit": 20}}
```

## Roles and gates

`runner`: user. The shipped gate is the default policy. Before any write, reread the live rule with [`system__describe_tool`](reference/tools/system/describe_tool). Every gated write parks the turn until a person permitted by its live approver rule decides; do not retry, use a twin spelling or edit policy to evade it. Conduct gated authoring inside a workspace session, with a human approval in the console or bound channel.

| write | shipped gate | who approves | default card preview |
|---|---|---|---|
| `system__create_graph` | required | a person allowed by the live approver rule | id, node kinds/ids/references, edges and iteration cap; update also names target id |
| `system__update_graph` | required | a person allowed by the live approver rule | id, node kinds/ids/references, edges and iteration cap; update also names target id |
| `system__delete_graph` | required | a person allowed by the live approver rule | target id |
| `crud__create_graph` | required | a person allowed by the live approver rule | id, node kinds/ids/references, edges and iteration cap; update also names target id |
| `crud__update_graph` | required | a person allowed by the live approver rule | id, node kinds/ids/references, edges and iteration cap; update also names target id |
| `workspaces__create_workspace_session` | none | none by default | no approval card |
| `system__create_graph_thread` | none | none by default | no approval card; optional bookkeeping only |

Prompts, descriptions and templates are withheld from the default graph preview; an allowed approver can inspect the full pending call. Reads and graph invocation ship ungated; an invoked node's own tool can still park on its live approval gate.

[`system__describe_tool`](reference/tools/system/describe_tool) reads the live gate for [`system__create_graph`](reference/tools/system/create_graph):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "create_graph"}}
```

[`system__describe_tool`](reference/tools/system/describe_tool) reads the live gate for [`system__update_graph`](reference/tools/system/update_graph):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "update_graph"}}
```

[`system__describe_tool`](reference/tools/system/describe_tool) reads the live gate for [`system__delete_graph`](reference/tools/system/delete_graph):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "delete_graph"}}
```

[`system__describe_tool`](reference/tools/system/describe_tool) reads the live gate for [`crud__create_graph`](reference/tools/crud/create_graph):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "crud", "tool_name": "create_graph"}}
```

[`system__describe_tool`](reference/tools/system/describe_tool) reads the live gate for [`crud__update_graph`](reference/tools/crud/update_graph):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "crud", "tool_name": "update_graph"}}
```

[`system__describe_tool`](reference/tools/system/describe_tool) reads the live gate for [`workspaces__create_workspace_session`](reference/tools/workspaces/create_workspace_session):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "workspaces", "tool_name": "create_workspace_session"}}
```

[`system__describe_tool`](reference/tools/system/describe_tool) reads the live gate for [`system__create_graph_thread`](reference/tools/system/create_graph_thread):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "create_graph_thread"}}
```

## Steps

1. Create the complete graph using [`system__create_graph`](reference/tools/system/create_graph). After the required approval, verify the stored topology. The Begin input is a nonempty string; the agent reads `nodes.begin.text`, and End adds `GRAPH-ANSWER:` to `nodes.answer.text`. That explicit transformation makes the End result distinct from the agent's intermediate token. A bounded cycle or callable router needs `max_iterations`.

```json
{"toolset_id": "system", "tool_name": "create_graph", "arguments": {"entity": {"id": "w7-graph", "description": "Pass a message to the review agent and collect its answer", "nodes": [{"kind": "begin", "id": "begin", "input_schema": {"type": "string", "minLength": 1}}, {"kind": "agent", "id": "answer", "agent_id": "w7-agent", "input_template": "{{ nodes.begin.text }}"}, {"kind": "end", "id": "end", "output_template": "GRAPH-ANSWER:{{ nodes.answer.text }}"}], "edges": [{"kind": "static", "from_node": "begin", "to_node": "answer"}, {"kind": "static", "from_node": "answer", "to_node": "end"}], "max_iterations": 8}}}
```

2. Reread with [`system__get_graph`](reference/tools/system/get_graph) and compare nodes, edges, templates and cap. A draft may save without a runnable Begin/End path; saving alone is not a runnability test. Session creation checks at least one node, exactly one Begin, a reachable End and any required cycle cap, while reference checks require readable node targets.

```json
{"toolset_id": "system", "tool_name": "get_graph", "arguments": {"id": "w7-graph"}}
```

3. To edit, preserve the whole readback, including every node and edge, then call [`system__update_graph`](reference/tools/system/update_graph). The id stays fixed. The turn parks for this distinct write; approval of create did not approve edit.

```json
{"toolset_id": "system", "tool_name": "update_graph", "arguments": {"id": "w7-graph", "entity": {"id": "w7-graph", "description": "Review one message through a bounded agent graph", "nodes": [{"kind": "begin", "id": "begin", "input_schema": {"type": "string", "minLength": 1}}, {"kind": "agent", "id": "answer", "agent_id": "w7-agent", "input_template": "{{ nodes.begin.text }}"}, {"kind": "end", "id": "end", "output_template": "GRAPH-ANSWER:{{ nodes.answer.text }}"}], "edges": [{"kind": "static", "from_node": "begin", "to_node": "answer"}, {"kind": "static", "from_node": "answer", "to_node": "end"}], "max_iterations": 10}}}
```

4. [`crud__create_graph`](reference/tools/crud/create_graph) and [`crud__update_graph`](reference/tools/crud/update_graph) are alternative checked write surfaces with the same required gates. Choose these instead of the corresponding system calls, never after a gate refusal to avoid waiting.

```json
{"toolset_id": "crud", "tool_name": "create_graph", "arguments": {"entity": {"id": "w7-graph", "description": "Pass a message to the review agent and collect its answer", "nodes": [{"kind": "begin", "id": "begin", "input_schema": {"type": "string", "minLength": 1}}, {"kind": "agent", "id": "answer", "agent_id": "w7-agent", "input_template": "{{ nodes.begin.text }}"}, {"kind": "end", "id": "end", "output_template": "GRAPH-ANSWER:{{ nodes.answer.text }}"}], "edges": [{"kind": "static", "from_node": "begin", "to_node": "answer"}, {"kind": "static", "from_node": "answer", "to_node": "end"}], "max_iterations": 8}}}
```

```json
{"toolset_id": "crud", "tool_name": "update_graph", "arguments": {"id": "w7-graph", "entity": {"id": "w7-graph", "description": "Review one message through a bounded agent graph", "nodes": [{"kind": "begin", "id": "begin", "input_schema": {"type": "string", "minLength": 1}}, {"kind": "agent", "id": "answer", "agent_id": "w7-agent", "input_template": "{{ nodes.begin.text }}"}, {"kind": "end", "id": "end", "output_template": "GRAPH-ANSWER:{{ nodes.answer.text }}"}], "edges": [{"kind": "static", "from_node": "begin", "to_node": "answer"}, {"kind": "static", "from_node": "answer", "to_node": "end"}], "max_iterations": 10}}}
```

5. Start an autonomous graph-bound workspace session through [`workspaces__create_workspace_session`](reference/tools/workspaces/create_workspace_session). Pass the Begin contract's string value as `graph_input`; `initial_instructions` is not a replacement for an explicit input. The graph uses each node agent's usable default profile unless a binding/node override selects another usable profile.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "ws-example", "binding": {"kind": "graph", "graph_id": "w7-graph"}, "graph_input": "Give one concrete check for a safe release.", "auto_start": true, "autonomous": true}}
```

6. Carry the returned session id into [`workspaces__get_workspace_session`](reference/tools/workspaces/get_workspace_session) and [`workspaces__read_workspace_session_messages`](reference/tools/workspaces/read_workspace_session_messages). The worked `sess-example` is used only as that returned id. Poll with a deadline and read the durable terminal state and End output; a `running` response alone proves neither work nor success. A yielded approval or question needs the human response, not a second invocation.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-example", "session_id": "sess-example"}}
```

```json
{"toolset_id": "workspaces", "tool_name": "read_workspace_session_messages", "arguments": {"workspace_id": "ws-example", "session_id": "sess-example", "tail": true, "limit": 200}}
```

7. Alternatively, from an agent already running in a workspace session, use [`workspace_ext__invoke_graph`](reference/tools/workspace_ext/invoke_graph). This runs a nested graph in that caller's session and returns `output`; use its actual result and the parent session transcript. A standalone MCP call has no workspace-session services and is refused with bad-request. This is an alternative execution mode, not a way to resume the graph session from step 5.

```json
{"toolset_id": "workspace_ext", "tool_name": "invoke_graph", "arguments": {"graph_id": "w7-graph", "input": "Give one concrete check for a safe release."}}
```

8. Session runs and nested invocations persist their state under the workspace session; they do not create GraphThread rows. [`system__create_graph_thread`](reference/tools/system/create_graph_thread) only stores a bookkeeping row supplied by a process, and [`system__get_graph_thread`](reference/tools/system/get_graph_thread) only reads such a row. Writing one does not run a graph or prove an outcome. Follow [graph threads](graphs/threads) only when a storage-backed process actually records its runs there; use session state and transcript for this flow.

9. After every session targeting the graph has ended and no graph/subscription reference remains, deliberately delete the disposable graph with [`system__delete_graph`](reference/tools/system/delete_graph). The required gate parks again. Deleting the definition does not delete its workspaces, session transcripts or separately stored thread rows. Keep the node agent until the graph is gone.

```json
{"toolset_id": "system", "tool_name": "delete_graph", "arguments": {"id": "w7-graph"}}
```

## Verification

Reread [`system__get_graph`](reference/tools/system/get_graph) after each approved write, including owner, node references, templates, input schema, edges and cap. The run must end with `ended_reason=completed`; the transcript's End `assistant_token` carries the rendered answer and `end_node_id`, followed by a graph-end `done`. A cap/error/cancellation is a failure, not an empty successful answer. The reader can also return raw Message rows; inspect event records by their supported model instead of assuming every row has `kind`.

For nested invocation, verify the actual `output` tool result and the parent session's completed turn. For optional cleanup, [`system__find_graphs`](reference/tools/system/find_graphs) must show no matching visible definition after approval.

```json
{"toolset_id": "system", "tool_name": "find_graphs", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "id"}, "op": "=", "right": {"kind": "value", "value": "w7-graph"}}, "limit": 20}}
```

## Refusal and failure

| result | meaning and next move |
|---|---|
| approval_required, approval-required or yielded approval | a session parks and a person decides; MCP without a park surface refuses the gated call; do not retry or invoke a second run |
| not-found or forbidden | absent/unreadable reference, access level, role or profile/toolset use; check the named precondition with its owner |
| validation-error | invalid shape, topology, Begin input or a draft that is not runnable; correct the named contract before a new intentional write/run |
| conflict | duplicate id, harness-managed row or `in_use_by` graph/session/subscription references; remove intended references, never bypass the guard |
| template_error or begin_input_invalid | the input/template contract disagrees; use `initial_input`, exact keys and the stored schema |
| max_iterations_exceeded, tool_turn_cap or failed | the run did not complete; inspect session error/ended detail and report the cause |
| bad-request | nested invocation has no workspace-session context, a missing graph or excessive invocation depth; use the supported execution mode |
| an invoked error result | rejection, timeout, cancellation or a failed child graph; do not turn it into a successful empty output |

## Related

- [Create and edit graphs](graphs/create-and-edit): topology, templates and replacement.
- [Run a graph](graphs/run-a-graph): session input and result records.
- [Graph threads](graphs/threads): the separate storage-backed bookkeeping surface.
- [Create an agent](runbooks/create-an-agent): usable node definitions.
- [Manage a workspace](runbooks/manage-a-workspace): workspace lifecycle and deliberate destruction.
