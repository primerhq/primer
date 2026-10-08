---
slug: agents
title: Agents - definitions and runtime
summary: How to define and invoke agents - the Agent entity, prompt structure, tool sets, response formats, auto-compaction, and how a session or a graph node wraps the turn loop.
related: [graphs, sessions, workspaces, tool-approval, yielding]
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
run by themselves - they're invoked inside a context (a session, a
graph node, a fresh-session subscription) and that context owns the
LLM loop and history persistence.

A session is the context. It covers both the human-in-the-loop case
and long-running headless work, because those differ in whether
anyone is watching rather than in machinery: the same `AgentExecutor`
style turn loop with tool dispatch, auto-compaction and stream
fan-out, committing LLM history to the workspace's `.state` repo as
git commits.

A turn is: the agent receives a user-ish input (a steer, an
instruction in a session start, a graph context payload
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
- `model` - `{profile_id}`: the id of a stored `ModelProfile`, which carries
  the provider and the model name. Creating or updating an agent that names a
  profile that does not exist is refused (422 `model_profile_not_found`, field
  `model.profile_id`); the system `create_agent` / `update_agent` tools answer
  the same as a `validation-error`. An update checks the profile only when it
  changes it.
- `temperature` - optional sampling temperature; `null` defers to
  the LLM adapter's default.
- `tools` - a list of scoped tool id strings, each of the form
  `<toolset_id>__<tool_name>` (e.g. `system__list_agents`). The runtime
  exposes exactly the listed tools - never a whole toolset - and an
  empty list means no tools. (Workspace tools are not listed here;
  they are composed onto the agent automatically when it attaches to
  a workspace.) Every toolset a tool id names must resolve: it is a built-in
  toolset (`system`, `workspaces`, `workspace_ext`, `misc`, `web`,
  `harness`, `trigger`, `collections`, `crud`) or a stored `Toolset` row
  (`search` is a stored row). A tool id with no `__` is checked as its own
  toolset at create and in `/status`, but the runtime skips such an id when it
  resolves an agent's toolsets, so it never provides a tool: always write
  `<toolset_id>__<tool_name>`. A create naming a toolset
  that does not exist is refused (422 `toolset_not_found`, field `tools`,
  every missing id named once); an update refuses only a toolset it ADDS, so
  an agent whose toolset was deleted later can still be edited.
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

1. Reconstruct the message history from `.state` git commits,
   folding compaction and rewind markers.
2. Call the LLM with the history + tool definitions.
3. Stream tokens; commit to `.state`.
4. When a tool_use stop arrives: dispatch each tool. For each
   call: validate args, check tool approval, dispatch handler, get
   result. Persist `tool_call` + `tool_result` rows.
5. Loop step 2 with extended history.
6. When a non-tool stop arrives: persist the assistant message, end
   the turn.

Auto-compaction:
before each turn (and again on a hard context overflow), the executor
estimates the size of the prompt: the history plus the part that goes out on
every call whatever the history is, the system prompt and the tool schemas
(an agent with many or large tools has a big fixed part, and it counts). If
that is over 90% of the model's context window minus the output reserve, run
a compaction strategy - typically summarise the head, keep the tail. The
tail always includes the input the model has not answered yet and never
splits a tool call from its results. The resulting summary replaces the
elided range in the reconstructed history, followed by the kept tail. The
original messages stay in storage; the substitution happens at history-
reconstruction time. A single turn that reads many files is compacted too:
its early tool rounds are summarised and the summary sits after the question,
so the question stays first and verbatim.

Compaction can come back without shrinking anything, and says so in the
session record: a `compaction_marker` carries `outcome` (`summarised`, or
`insufficient` when the prompt is still over the trigger afterwards), and a
`compaction_note` record is written when no marker was (`unreducible`: nothing
could be summarised, for example when the system prompt and tool schemas alone
fill the window; `skipped`: the trigger cannot be reached and the prompt still
fits, or the last compaction already left it at about this size, so the
summary is not summarised again before the prompt has grown; noted once per
run, not every turn). If the summarising call itself is rejected as too large (the history it has to summarise is over the model's window),
the compaction retries once, without tools, on a shortened input: tool outputs are left out first, then the rest is
summarised in up to four pieces, one after another, each with the summary so far. The marker records this as
`summary_input_reduced`, the manual compact response carries it, and the console's compaction divider says the
summary read a reduced input and what was cut. An agent whose compaction uses tools and whose tool loop overflows in a
later round ends with the summary it had already written, and the marker and the divider say in which round. If even that cannot fit, or the retry is rejected too, the turn (or the manual compact
request) fails with `summariser_overflow` (the same 413 as `context_overflow_unrecoverable`): compact earlier, or
use a larger-context model. If the provider still rejects a call as too large (a rejected request,
or one a provider streams back as an error before any output; both are
handled the same way), the
turn is compacted once more and CONTINUED from the tool rounds it already ran
(no tool runs twice; a long turn's early rounds are summarised, the question
and the newest round are kept). If compaction cannot shrink the prompt, or the
replay is rejected too, the completed rounds are recorded (cut down, with a
note that the calls already ran) and the turn ends with
`context_overflow_unrecoverable` instead of being replayed unchanged: shorten
the system prompt, give the agent fewer tools, or use a larger-context model.
The turn ends the same way, without compacting anything, when the agent's
`max_output_tokens` is not below the model's context window and the provider
rejects the call as a prompt that does not fit (for example with the
`context_length_exceeded` code): no history, however short, fits beside that
cap, so lower `max_output_tokens` (or use a model with a larger window). A
provider that words the same situation as an output-cap error (vLLM's
`'max_tokens' is too large: 32768 ... maximum context length is 32768`,
Anthropic's `max_tokens: N > M`) is not an overflow at all: nothing is compacted
either, but the turn ends with the provider's own error, not
`context_overflow_unrecoverable`: a plain HTTP 400 `bad-request` problem with no
`ended_detail` when the provider's adapter raised it (vLLM, Anthropic), or the
stream error's own code as `ended_detail` (`bad_request`) when the adapter
reported it as a stream error (Ollama, Gemini).

Primer warns about such a cap before a turn is lost to it, and does not refuse it
(some servers clamp an oversized cap instead of rejecting it, so a refusal would
break setups that work): `GET /v1/agents/{id}/status` returns a `warnings` list
next to `issues` (a warning does not make `ok` false, and the agent page shows
it), and a turn logs a warning at its start. For an aggregated profile the check
uses the largest member window, so it warns only when no member could ever take
the call.

Streaming: subscribers (workspace tap clients, internal taps) see token
events in the order the LLM produces them. Persisted state is the
complete messages, not the token-by-token stream - reconnect
replays the complete messages, not the tokens.

## Lifecycle and states

An Agent row has no lifecycle of its own - it's a CRUD entity.
**Agent invocations** have lifecycle, but that lifecycle is owned
by the wrapping session. See [sessions](sessions.md)
and [sessions](sessions.md).

What's worth knowing:

- The agent definition is **re-read on every turn**. Edit the
  agent's prompt or toolset list mid-session; next turn sees the
  edit. This is sometimes the feature (hot-config) and sometimes
  the bug (tool disappeared).
- Structured output is a graph-node feature: a graph node with an
  `output_schema` populates `NodeOutput.parsed`; a session returns
  only the text.
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
  (`{profile_id}`, the id of a stored ModelProfile), `tools` (list of
  `<toolset_id>__<tool_name>` strings), optional `temperature` and
  `max_tool_turns` (default 50). Omit `id` and the server assigns
  `agent-<hex>` (e.g. `agent-3f9a1c8d`); supply one to use it
  verbatim. The id is immutable after creation.
- `system::update_agent` - partial update. Editing or deleting a
  harness-managed agent (`harness_id` set) returns a `conflict`
  error, and `create_agent` refuses a body that sets `harness_id`
  (`bad-request`); the same rule as the REST routes.
- `system::delete_agent` - cascade-blocked if any session is bound to
  the agent. Deleting the seeded `operator` or `builder` agent is
  allowed but marks the install as not set up (admins are sent to the
  setup checklist, every other user waits on a setup screen) until
  `POST /v1/setup/seed` or the next server start re-creates it with its
  default definition, not your edits.
- `system::find_agents` - predicate query.

### Model profiles (system toolset)

An agent runs under a **ModelProfile**, not under a provider and a model
name: the profile is the registry of what a provider can serve. A single
profile carries `provider_id` (a stored LLM provider), `model_name` (the
provider-side wire name), `context_length` and an optional `config`
(for example `reasoning`); an aggregated profile (`kind: "aggregated"`) lists
two or more other profiles in `members` to fail over across. The ids in this
documentation follow the convention `<provider id>--<model name>` (for
example `anthropic-1--claude-sonnet-4-6`); primer does not enforce it.

- `system::create_model_profile`, `system::get_model_profile`,
  `system::list_model_profiles`, `system::find_model_profiles`,
  `system::update_model_profile`, `system::delete_model_profile` - admin
  role for the writes (a profile is provider configuration). A single
  profile whose provider does not exist is refused, and a profile that an
  agent uses cannot be deleted.

Create the profile before the agent that names it:

```json
{
  "tool": "system::create_model_profile",
  "arguments": {
    "entity": {
      "id": "lp-claude--claude-sonnet-4-6",
      "description": "Claude Sonnet on the lp-claude provider",
      "provider_id": "lp-claude",
      "model_name": "claude-sonnet-4-6",
      "context_length": 200000
    }
  }
}
```

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
    "entity": {
      "id": "summarise-document",
      "description": "Summarises a document file into 200 words or fewer.",
      "system_prompt": [
        "You receive a document file path as input. Read the file, produce a concise summary (max 200 words), and write it to summary.md in the same directory."
      ],
      "model": {"profile_id": "lp-claude--claude-sonnet-4-6"},
      "tools": ["system__get_document_content"],
      "max_tool_turns": 5
    }
  }
}
```

The profile `lp-claude--claude-sonnet-4-6` must exist (see "Model profiles"
above); a `create_agent` that names a profile that is not stored is refused.

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

3. Use it - either spin up a session or instantiate a graph that uses
   it as a node.

## Gotchas

- **Tool dispatch errors don't crash the agent.** A tool that
  throws → `ToolResultPart(error=True)` fed back to the LLM. The
  LLM sees the error and (usually) recovers. This is the
  recovery loop you may have seen as "invalid arguments for X" in
  session logs.
- **Auto-compaction triggers between turns at 90% context, counting
  the system prompt and tool schemas.** Don't assume the LLM's history
  input is a contiguous slice of stored rows - it can have a compaction
  summary replacing a range, and the summary can sit after the first
  user message of the turn rather than in front.
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
  node's `output_schema` populates `NodeOutput.parsed`; a session
  returns text only.
- **`max_tool_turns` is a safety cap, not a quality control.** It
  caps tool-call rounds within a turn to stop runaways. Setting it
  too low causes legitimate multi-step work to fail; too high lets
  pathological loops burn tokens. The default is 50. `max_tool_turns=N`
  runs at most N-1 tool rounds: the round that reaches N is not run, each
  of its tool calls is answered with an error result ("not executed:
  tool-turn cap reached") so the session stays usable. An interactive
  session then rests waiting for your next message (it does not resume by
  itself, including after a restart; the turn's last `done` record has
  `stop_reason` `tool_turn_cap`, which the console shows as "stopped at the
  tool-turn cap"); an autonomous agent session (for
  example one fired by a trigger) ends with `ended_reason`
  `tool_turn_cap`, and a new message reopens it with a fresh round count.
  A session bound to a channel thread is told it stopped short (a message
  saying it stopped at its tool-turn cap, with what it had so far), in all
  three cases, and the turn is counted as `tool_turn_cap` in the turn metrics,
  not as `completed`.
  A graph agent node that hits its own cap FAILS with `ended_detail`
  `tool_turn_cap` (the run ends `failed`; its history stays
  valid: every call of the capped round is answered), and a subagent that
  hits its cap answers `invoke_agent` with an ERROR result
  (`{"type": "tool-turn-cap", "message", "partial_output"}`) instead of its
  last text as a success: `partial_output` is what it had said so far, for
  the calling agent to use or discard.
- **Tool calls can yield.** A tool returning `Yielded(...)` parks
  the agent. See [yielding](yielding.md) for what happens then.
  Outside primer (over MCP) yielding tools are invisible.
- **Approval-gated tools surface as silent parks.** From inside
  the agent, a `_approval` yield happens transparently - the
  agent's next message contains the tool result (or rejection).

## Related

- [graphs](graphs.md) - graphs orchestrate multiple agents.
- [sessions](sessions.md) - the headless wrapper.
- [workspaces](workspaces.md) - what sessions run inside.
- [tool-approval](tool-approval.md) - gating individual tool calls.
- [yielding](yielding.md) - the park/resume primitive.
