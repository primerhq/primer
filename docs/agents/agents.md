---
slug: agents
title: Agents - definitions and runtime
summary: How to define and invoke agents - the Agent entity, prompt structure, tool sets, response formats, auto-compaction, and the differences between chat-mode and session-mode execution.
related: [graphs, chats, sessions, workspaces, tool-approval, yielding]
mcp_tools:
  - system::list_agents
  - system::get_agent
  - system::create_agent
  - system::update_agent
  - system::delete_agent
  - system::find_agents
  - search::search_agents
---

# Agents - definitions and runtime

## Overview

An **Agent** is primer's atomic unit of "an LLM with a job". Every
agent is a stored row describing: a system prompt (one or more
string segments), the LLM provider/model that runs it, the scoped
tools it has access to, an optional sampling temperature, and a
max-tool-turns cap. Agents don't
run by themselves - they're invoked inside a context (a chat, a
session, a graph node, a fresh-session subscription) and that
context owns the LLM loop and history persistence.

There are two contexts in v1: chats (multi-turn human-in-the-loop)
and sessions (long-running headless work). The underlying executor
machinery is shared - both run the same `AgentExecutor` style turn
loop with tool dispatch, auto-compaction, and stream fan-out - but
the wrapping differs (chat persists every message as a row in
ChatMessage; session commits LLM history to the workspace's `.state`
repo as git commits).

A turn is: the agent receives a user-ish input (a user_message in
chat, an instruction in a session start, a graph context payload
in a graph node), the LLM generates tokens, the executor parses
tool calls out of the stream and dispatches them, tool results
land back in the LLM history, the LLM continues until it stops
with a non-tool message. That stop is the end of a turn. The next
turn begins with the next user input.

Agents are the primary indexed entity in the semantic catalogue. An
agent's description + system_prompt are embedded into
`_internal_agents`; `search::search_agents` is the discovery path.
This is why agent descriptions matter for usability - they're
what shows up in semantic search results.

Use a single agent when one LLM with one toolset does the whole
job; not when you need to chain several agents with conditional
routing between them (use a [graph](graphs.md) via
`system::create_graph`).

## Mental model

An `Agent` row carries:
- `id`, `description` (free text; embedded for search).
- `system_prompt` - a list of strings, joined by the runtime at
  invoke time (adapters that support a multi-segment system prompt
  emit it directly; others join the segments with blank lines).
  Splitting into fragments lets the operator inject context (e.g. a
  workspace-specific preamble) without rewriting the base prompt.
- `model` - `{provider_id, model_name}`. The provider must exist as
  an `LLMProvider` row.
- `temperature` - optional sampling temperature; `null` defers to
  the LLM adapter's default.
- `tools` - a list of scoped tool id strings, each of the form
  `<toolset_id>__<tool_name>` (e.g. `system__list_files`). The runtime
  exposes exactly the listed tools - never a whole toolset - and an
  empty list means no tools. (Workspace tools are not listed here;
  they are composed onto the agent automatically when it attaches to
  a workspace.)
- `max_tool_turns` - cap on tool-call rounds within a single turn
  before the turn is force-stopped (default 50; `null` means
  unbounded).
- `compaction_prompt` - optional list of strings guiding how the
  runtime compacts history; empty falls back to the default.
- `harness_id` - non-null for agents installed by a harness;
  blocks public CRUD.

Structured output is configured on a **graph node** (its
`output_schema`), not on the agent; the agent itself has no
`response_format` field.

The executor responsibilities, per turn:

1. Reconstruct the message history (chat: from rows + compaction
   markers; session: from `.state` git commits).
2. Call the LLM with the history + tool definitions.
3. Stream tokens; persist `assistant_token` rows (chat) or commit
   to `.state` (session).
4. When a tool_use stop arrives: dispatch each tool. For each
   call: validate args, check tool approval, dispatch handler, get
   result. Persist `tool_call` + `tool_result` rows.
5. Loop step 2 with extended history.
6. When a non-tool stop arrives: persist the assistant message, end
   the turn.

Auto-compaction (see [chats](chats.md) for chat-specific details):
before each LLM call, the executor counts tokens. If over 90% of
the model's context window, run a compaction strategy - typically
summarise the head, keep the tail. The resulting summary replaces
the elided range in the reconstructed history. The original
messages stay in storage; the substitution happens at history-
reconstruction time.

Streaming: subscribers (the WS connection, internal taps) see token
events in the order the LLM produces them. Persisted state is the
complete messages, not the token-by-token stream - reconnect
replays the complete messages, not the tokens.

## Lifecycle and states

An Agent row has no lifecycle of its own - it's a CRUD entity.
**Agent invocations** have lifecycle, but that lifecycle is owned
by the wrapping context (chat or session). See [chats](chats.md)
and [sessions](sessions.md).

What's worth knowing:

- The agent definition is **re-read on every turn**. Edit the
  agent's prompt or toolset list mid-session; next turn sees the
  edit. This is sometimes the feature (hot-config) and sometimes
  the bug (tool disappeared).
- Structured output is a graph-node feature: a graph node with an
  `output_schema` populates `NodeOutput.parsed`; chat and session
  contexts return only the text.
- Agents can call other agents. Either statically (a graph node
  invokes another agent) or dynamically via the system toolset
  if the operator allowlists it.

## MCP tools

Agents are managed via standard CRUD plus the semantic search tool.

### CRUD (system toolset)

- `system::list_agents` - paginated.
- `system::get_agent` - fetch the row including `system_prompt`,
  `tools`, `response_format`, `llm`.
- `system::create_agent` - body fields: optional `id`,
  `description`, `system_prompt` (list of strings), `model`
  (`{provider_id, model_name}`), `tools` (list of
  `<toolset_id>__<tool_name>` strings), optional `temperature` and
  `max_tool_turns` (default 50). Omit `id` and the server assigns
  `agent-<hex>` (e.g. `agent-3f9a1c8d`); supply one to use it
  verbatim. The id is immutable after creation.
- `system::update_agent` - partial update. Editing a harness-
  managed agent (`harness_id` set) returns 409.
- `system::delete_agent` - cascade-blocked if any chat references
  the agent.
- `system::find_agents` - predicate query.

### Discovery (search toolset)

- `search::search_agents` - semantic search over agent
  description + system_prompt. Returns ranked agent ids.

## Workflows

### Workflow 1 - define an agent and invoke it in a fresh session

**Goal.** Create the `summarise-document` agent and run it once
in a fresh workspace.

1. Create the agent:

```json
{
  "tool": "system::create_agent",
  "arguments": {
    "id": "summarise-document",
    "description": "Summarises a document file into 200 words or fewer.",
    "system_prompt": [
      "You receive a document file path as input. Read the file, produce a concise summary (max 200 words), and write it to summary.md in the same directory."
    ],
    "model": {
      "provider_id": "lp-claude",
      "model_name": "claude-sonnet-4-6"
    },
    "tools": ["system__get_document_content"],
    "max_tool_turns": 5
  }
}
```

2. Find a workspace to run in:

```json
{
  "tool": "workspaces::list_workspaces",
  "arguments": {"limit": 10}
}
```

3. Create the session:

```json
{
  "tool": "workspaces::create_workspace_session",
  "arguments": {
    "workspace_id": "ws-default",
    "binding": {"kind": "agent", "agent_id": "summarise-document"},
    "initial_instructions": "Summarise the document with id 'doc-readme'.",
    "auto_start": true
  }
}
```

Response threads the session `id` and a `status` of `running`:
```json
{"id": "ses_8f2a", "status": "running"}
```

4. Poll `workspaces::get_workspace_session` with `{"workspace_id": "ws-default", "session_id": "ses_8f2a"}` until `status` is `ended` (see [sessions](sessions.md)).

### Workflow 2 - discover an existing agent by capability

**Goal.** Connected agent doesn't know what's available. Find an
agent that does code review.

1. Search:

```json
{
  "tool": "search::search_agents",
  "arguments": {"query": "code review lint static analysis", "top_k": 5}
}
```

Returns hits ranked by description similarity. Top hits
typically include both a `review-code` agent and a `lint-pr`
agent depending on what's installed.

2. Inspect the best candidate:

```json
{
  "tool": "system::get_agent",
  "arguments": {"id": "review-code"}
}
```

Read the description + system_prompt to confirm fit.

3. Use it - either spin up a session or fire a chat or instantiate
   a graph that uses it as a node.

## Gotchas

- **Tool dispatch errors don't crash the agent.** A tool that
  throws → `ToolResultPart(error=True)` fed back to the LLM. The
  LLM sees the error and (usually) recovers. This is the
  recovery loop you may have seen as "invalid arguments for X" in
  session logs.
- **Auto-compaction triggers between turns at 90% context.** Don't
  assume the LLM's history input is a contiguous slice of stored
  rows - it can have a compaction summary replacing a range.
- **Streaming tokens are not persisted as separate rows.** Only
  complete messages (assistant_message, tool_call, tool_result)
  land in storage. The token-by-token stream is observed by live
  subscribers only.
- **Agent definitions are re-read every turn.** Edit the agent
  while a session is running; the next turn sees the edit. The
  tool set, the prompt, the response_format - all re-resolved.
- **`harness_id` makes an agent immutable through CRUD.** Use
  `harness::harness__sync` after upstream changes, not
  `system::update_agent`.
- **Structured output lives on graph nodes, not agents.** A graph
  node's `output_schema` populates `NodeOutput.parsed`; chat and
  session contexts return text only.
- **`max_tool_turns` is a safety cap, not a quality control.** It
  caps tool-call rounds within a turn to stop runaways. Setting it
  too low causes legitimate multi-step work to fail; too high lets
  pathological loops burn tokens. The default is 50.
- **Tool calls can yield.** A tool returning `Yielded(...)` parks
  the agent. See [yielding](yielding.md) for what happens then.
  Outside primer (over MCP) yielding tools are invisible.
- **Approval-gated tools surface as silent parks.** From inside
  the agent, a `_approval` yield happens transparently - the
  agent's next message contains the tool result (or rejection).

## Related

- [graphs](graphs.md) - graphs orchestrate multiple agents.
- [chats](chats.md) - the multi-turn human-in-the-loop wrapper.
- [sessions](sessions.md) - the headless wrapper.
- [workspaces](workspaces.md) - what sessions run inside.
- [tool-approval](tool-approval.md) - gating individual tool calls.
- [yielding](yielding.md) - the park/resume primitive.
