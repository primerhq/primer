---
title: "Invoke another agent"
summary: "Run a subagent on a prompt and take its text back, with the depth limit, the subagent identity and the cap error."
order: 5
tools: [system__invoke_agent]
related: [agents/switch-and-binding, agents/compaction]
---
## What it is

`system__invoke_agent` runs another agent once on a prompt and returns its text. It is the subagent path: the call blocks, the caller's turn does not continue until the subagent finishes (or the turn parks), and the result is `{"output": <text>}`. Use it for a self-contained subtask. For handing the rest of the conversation off, use `system__switch_to_agent` instead.

## Concepts

- **Stateless.** The subagent gets its own `system_prompt` plus the one prompt it was invoked with. It shares no history with the caller, so the prompt is the whole brief: the task, the inputs (or where to find them), and the shape the answer should have.
- **Its own tools only.** The subagent runs with its full `agent.tools` surface, including yielding tools, and never with the caller's workspace file tools. If the call happens inside a workspace session, the subagent still gets that workspace's `agents.md` instructions, like any agent running there.
- **Identity.** The subagent acts as the caller: the run's identity is threaded through to it. A call with no known caller (an MCP call with no calling session) ranks the subagent as an ordinary user, never as the system.
- **Depth.** Every invocation adds a level to the invocation depth. The depth is capped (8 by default, `PRIMER_MAX_INVOCATION_DEPTH`), and a call that would exceed the cap errors with a bad-request naming the depth: an agent invoked through an agent invoked through an agent stops at the cap.
- **The tool-turn cap.** A subagent that stops at its own `max_tool_turns` did not finish. Its last text is not returned as a success: the call is an error result of the shape `{"type": "tool-turn-cap", "message", "agent_id", "max_tool_turns", "partial_output"}`, where `partial_output` is what it had said so far, for the calling agent to use or discard.
- **A Stop beats everything.** If the caller's turn is Stopped while the subagent runs, the subagent's turn ends too. A subagent turn that ends on the Stop is re-cancelled: it never hands the caller a partial answer as the call's real result.
- **A failed model call is an error.** If the subagent's LLM stream fails, the call answers a provider-error result, not a successful empty output.
- **Yielding parks the chain.** If a subagent's tool yields (an approval gate fires, or `system__ask_user` parks), the whole nested invocation chain parks: the in-progress turn's messages are captured onto the yield's frame stack, the worker parks the session, and when it resumes the subagent keeps counting from where it parked.

## How to

1. Pick the agent id: find it with `system__find_agents` or read its live map page under `this-install/agents/` in the wiki.
2. Write the prompt as a self-contained brief. The subagent sees nothing else.
3. Call `system__invoke_agent`. On success, use `output`. On an error result, read the message and, for a cap, the `partial_output`.
4. Work that needs the workspace file and shell belongs in a workspace session, not in a subagent: the subagent does not inherit the caller's file tools.

## Tool calls

### system__invoke_agent

```json
{"toolset_id": "system", "tool_name": "invoke_agent", "arguments": {"agent_id": "agent-researcher", "prompt": "Summarise the RFC."}}
```

Reference: [`system__invoke_agent`](reference/tools/system/invoke_agent).

## Gotchas

- It is not a handoff. The caller keeps the conversation and gets one text back; the switch tools hand the session itself over.
- A vague prompt earns a vague answer. There is no shared history to fall back on, and nothing re-asks.
- The depth cap is on the chain, not per agent: `system__invoke_agent` and `workspace_ext__invoke_graph` both raise the same counter, so eight levels of nested agent and graph invocations is the ceiling.
- The cap error is an error, not a result. `partial_output` is the subagent's text so far, not its answer; treat the call as not finished.
- An `ask_user` inside a subagent parks the whole chain. The operator answers under the caller's session, and the answer comes back to the subagent as that call's result.
- The subagent's `max_tool_turns` is its own, not the caller's. A caller with a generous cap can still trip a subagent with a small one.

## Related

[Switch agents and the binding](agents/switch-and-binding), [Compaction](agents/compaction).
