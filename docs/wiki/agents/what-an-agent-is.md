---
title: "What an agent is"
summary: "An agent is a stored row: a model profile, a system prompt, a tool grant and caps, run by the turn loop inside a session."
order: 1
related: [agents/create-and-edit, agents/compaction, sessions/what-a-session-is]
---
## What it is

An agent is a stored row that binds an LLM to a job: the model profile that runs it, the system prompt that shapes it, the list of tools it may call, and the caps that keep a run from running away. The row does not run by itself. It is invoked inside a session (or the agent node of a graph), and the session's turn loop does the work.

## Concepts

- **Definition versus run.** The row is a definition and has no lifecycle of its own. A run is a turn inside a session; the session owns the run's lifecycle (park, Stop, cancel, compaction) and the agent row is just what the loop reads.
- **A turn.** The executor calls the model with the history and the agent's tools, dispatches every tool call the model makes, feeds the results back, and repeats until the model stops with a message that calls no tools. That stop ends the turn; the next input starts the next one.
- **The row is re-read every turn.** The executor re-reads the agent on every turn. Edit the prompt or the tools while a session is running and the next turn sees the edit. Sometimes that is the feature (hot config) and sometimes it is the bug (a tool disappeared from the grant).
- **Tools are scoped grants.** The `tools` list names individual tools as `<toolset_id>__<tool_name>`; the runtime exposes exactly the listed tools, never a whole toolset. An empty list is an agent with no tools. See [Tools and grants](agents/tools-and-grants).
- **Prompt parts are rendered, not concatenated.** The `system_prompt` list is joined and rendered as a template against the run's execution context, and the workspace and session sections are appended after it. See [Prompt parts and the execution context](agents/prompts-and-parts).
- **Discovery.** Every agent has a live map page at `this-install/agents/<id>` in the wiki's system collection, holding its description and model profile. The description is what a search matches, so write it like an index entry.

## Gotchas

- Tool dispatch errors do not crash the agent. A tool that throws feeds its error text back to the model as the call's result, and the model usually recovers. That is the "invalid arguments for X" loop you may see in session logs.
- Streaming tokens are not stored. Only complete messages (the assistant message, tool calls and tool results) land in the session's history.
- A session returns text only. Structured output is set on a graph node's `output_schema` or on the session's response format, not on the agent's run.
- Compaction is a session mechanism. When a session's prompt nears the model's context window, the session compacts its own history; the agent's `compaction_prompt` and `compaction_tool_access` steer how. See [Compaction](agents/compaction).

## Related

[Create and edit agents](agents/create-and-edit), [Compaction](agents/compaction), [What a session is](sessions/what-a-session-is).
