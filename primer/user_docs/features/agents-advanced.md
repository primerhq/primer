---
slug: agents-advanced
title: Agents (advanced)
section: features
summary: Model selection, system and compaction prompts, fine-grained tool binding, and the retry and turn loop behavior.
---

## Overview

The `features/agents` walkthrough covers creating an agent and the first invocation. This page covers day-two configuration: swapping models, writing effective system prompts, narrowing tool access below the toolset level, and understanding what the retry knobs actually control.

## Model selection

Every agent declares one provider and one model. To change the model:

1. Open the agent detail and click **Edit** on the Config tab.
2. Change the **LLM provider** or **Model** dropdown in the Basic tab.
3. Click **Save changes**.

Sessions started after the save use the new model. Any session already in flight keeps the old model until it ends.

```callout:tip
Prefer the smallest model that meets your quality bar. The cost difference between a large flagship model and a small fast model can exceed 10x for the same task. Reserve the largest models for prompts where the agent visibly fails to follow complex instructions.
```

## System prompt

The system prompt (Advanced tab) is the fixed instruction block prepended to every turn. It accepts Jinja-style placeholders resolved against the session's input metadata:

| Placeholder | Resolves to |
|---|---|
| `{{ agent.name }}` | The agent's ID |
| `{{ session.workspace_id }}` | Workspace the session runs in |
| `{{ session.input }}` | The initial input string |
| `{{ now }}` | Current ISO timestamp |

Keep the system prompt focused on role, output format, and constraints. Avoid embedding data that changes per-run -- pass that through the session input instead.

## Compaction prompt

When the conversation grows past the model's context window, the runtime compacts older turns into a summary. The compaction prompt (Advanced tab) controls what to keep.

Leave it blank to use the framework default, which preserves system context, recent turns, and pending tool calls. Override it only when your agent has a domain-specific retention need -- for example, a research agent that must preserve cited sources, or a coding agent that must retain the current file path under edit.

## Fine-grained tool binding

The Tools tab in the create/edit modal lets you select individual tools. You are NOT binding an entire toolset -- only the tools you explicitly check are exposed to the model. This matters for two reasons:

- **Scope control**: the `system` toolset ships over 100 tools. Binding all of them inflates the model's tool list, increasing token cost per turn and the chance of an unintended call. Pick the minimum set the agent needs.
- **Emergency deny**: remove a specific tool from the agent's list to deny it immediately without changing the underlying toolset or approval policy.

Two controls compose for layered access:

1. **Tool selection** (the modal): determines which scoped tool IDs (`toolset__tool`) are registered with the agent. Nothing outside this list can be called.
2. **Tool approval policies**: for calls that should route to a human gate before executing, configure an approval policy on the `(toolset, tool)` pair in the tool approval settings. The agent still sees the tool in its list but the runtime intercepts the call before dispatch.

## The turn and retry loop

Each session runs through a turn loop:

1. The runtime sends the current conversation to the model.
2. If the model returns tool calls, each call is dispatched to the appropriate toolset.
3. Tool results are fed back as the next message.
4. Steps 1-3 repeat until the model stops requesting tools or the session is paused or cancelled.

When a tool call errors, the agent receives the error text as the tool result and decides how to proceed. The runtime adds a retry layer on top:

- **Max attempts**: how many times the runtime retries a failed tool dispatch before returning the error to the model. Configured per-agent in the Advanced tab (if exposed) or via the API.
- **Backoff**: seconds between retry attempts.

Set max attempts to 1 (no retry) when tool errors are meaningful signals that the agent should reason about. Set it higher for transient network or service failures where silent retry is safe.

```callout:tip
Do not set a high retry count on tools that perform writes. A network timeout that retries three times may produce three writes if the first call succeeded but the response was lost.
```

## Temperature

Temperature (Advanced tab) controls model output randomness. Leave it blank to use the provider's default. Set it to a low value (0.1-0.3) for deterministic extraction or structured output tasks. Use higher values (0.7-1.0) for creative or open-ended generation.

## Automate this

```ref:reference/api-agents
Full resource schema and all configuration fields available via the API.
```
