---
title: "Compaction"
summary: "How a session compacts its own history before the prompt overflows the window, what the records say, and the 413 errors."
order: 7
related: [agents/what-an-agent-is, agents/switch-and-binding]
---
## What it is

Compaction is a session mechanism. Before the prompt overflows the model's context window, the session compacts its own history: it summarises the head and keeps the tail, so the turn fits. The agent's `compaction_prompt` and `compaction_tool_access` fields steer how the summariser works; the rest is owned by the session executor.

## Concepts

- **The trigger.** Before each turn, and again on a hard context overflow, the executor estimates the prompt: the history plus the part that goes out on every call whatever the history is, the system prompt and the tool schemas (an agent with many or large tools has a big fixed part, and it counts). When that is over 90% of the model's context window minus the output reserve, a compaction strategy runs: typically summarise the head, keep the tail.
- **The tail.** It always includes the input the model has not answered yet, and it never splits a tool call from its results. A single turn that reads many files is compacted too: its early tool rounds are summarised and the summary sits after the question, so the question stays first and verbatim.
- **The originals stay.** The original messages remain in storage; the substitution happens at history-reconstruction time, marked by the `compaction_marker` record. The summary replaces the elided range, followed by the kept tail.
- **The agent's part.** `compaction_prompt` is an optional list of strings that guide the summariser; empty falls back to the default. `compaction_tool_access` decides whether the tier-2 summarisation call carries the agent's tools in a bounded, ephemeral tool-use loop (the tool activity streams to the events but never enters the compacted history) or is plain text-only summarisation.
- **What gets recorded.** A `compaction_marker` record carries the `outcome`: `summarised`, or `insufficient` when the prompt is still over the trigger afterwards. It also records `summary_input_reduced` when the summariser's own call had to be made on a reduced input. When no marker was written, a `compaction_note` record says why: `unreducible` (nothing could be summarised, for example because the system prompt and tool schemas alone fill the window) or `skipped` (the trigger cannot be reached and the prompt still fits, or the last compaction already left it at about this size, so a summary is not summarised again before the prompt has grown). The note is written once per run, not every turn. The counted outcomes are `pruned` (the tier-1 prune of oversized tool outputs sufficed), `summarised`, `unreducible`, `skipped` and `insufficient`.
- **The summariser can overflow.** The summariser is a model call like any other, and the head it has to summarise can be over the model's window. The compaction then retries once, without tools, on a reduced input: tool outputs are left out first, then the rest is summarised in up to four pieces, one after another, each with the summary so far. If the compaction uses tools and its tool loop overflows in a later round, it ends with the summary it had already written, and the marker names the round.
- **The failures.** If even the reduced input cannot fit, or the retry is rejected too, the turn (or the manual compact request) fails with `summariser_overflow`, the same 413 as `context_overflow_unrecoverable`: compact earlier, or use a larger-context model. When the provider rejects the turn's own call as too large, the turn is compacted once more and CONTINUED from the tool rounds it already ran (no tool runs twice; a long turn's early rounds are summarised, the question and the newest round are kept). If compaction cannot shrink the prompt, or the replay is rejected too, the completed rounds are recorded with a note that the calls already ran, and the turn ends with `context_overflow_unrecoverable` (413): shorten the system prompt, give the agent fewer tools, or use a larger-context model. The turn ends the same way, without compacting anything, when the agent's `max_output_tokens` is not below the model's context window: no history, however short, fits beside that cap.
- **Where it runs.** Compaction is the session executor's job. A graph agent node and a `system__invoke_agent` subagent run the plain turn loop without it: their single turn cannot compact itself.
- **Manual compaction.** Compacting a session's transcript on demand is a console route with no agent tool: `POST /v1/workspaces/{workspace_id}/sessions/{session_id}/compact`. See [Not agent-callable yet](not-agent-callable).

## Gotchas

- Do not assume the LLM's history input is a contiguous slice of stored rows. It can have a compaction summary replacing a range, and the summary can sit after the first user message of the turn rather than in front.
- The fixed part counts. A prompt that will never compact is usually one whose system prompt and tool schemas alone fill the budget: that is the `fixed_over_budget` case, and no history shrinkage helps.
- `unreducible` is a failure with no marker; `skipped` is a deliberate no-op with no marker. Both leave the history unchanged and make no summariser call.
- A cancelled turn can still commit its forced compaction's marker: the marker commit finishes out-of-band, and the next turn rebuilds around it.
- The 413 codes name the fix. `summariser_overflow` says the history itself is over the window; `context_overflow_unrecoverable` says the prompt will not fit no matter what, and the problem details say how far recovery got (`forced_compaction`, `replay_attempted`, `persisted_rounds`, `summarised_rounds`).
- Graph nodes and subagents do not auto-compact. A long graph workflow that needs a short context has to pass digests between its nodes.

## Related

[What an agent is](agents/what-an-agent-is), [Switch agents and the binding](agents/switch-and-binding), [Not agent-callable yet](not-agent-callable).
