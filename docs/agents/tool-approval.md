---
slug: tool-approval
title: Tool approval policies
summary: Pre-dispatch gates that pause tool execution behind operator approval, policy evaluation, or LLM-judge calls.
related: [yielding, mcp-exposure, agents, chats]
mcp_tools: []
---

# Tool approval policies

## Overview

A **tool approval policy** is a row that says "before this tool is
allowed to run, run a gate." It exists because some tool calls are
risky enough that the operator wants visibility — or veto power —
before they execute. The mechanism is a generalisation of an
"are-you-sure" prompt, with three kinds of gate: required (a human
must click approve), policy (evaluate a Rego rule against the call
arguments), and llm (ask a judge model whether to allow). All three
share a common shape: at LLM-call time, before the tool actually
dispatches, the gate runs. If it says block, the call is parked
exactly like a yielding tool. If it says allow, dispatch proceeds.

A policy is identified by `(toolset_id, tool_name)` — a wildcard tool
name is not supported in v1; one row per concrete tool. Policies are
optional: a tool with no policy row dispatches immediately. The
policy lookup runs on every call (it's not cached on the tool's
descriptor) so flipping a policy on takes effect for the next call
without restart.

The thing that catches everyone is the interaction with MCP. The MCP
exposure layer treats "this tool has a `required`-type policy" as
"this tool cannot be exposed to MCP clients at all" — because MCP
has no pause/resume primitive. So if you add an approval policy to a
tool that was previously available over MCP, it silently disappears
from external `tools/list`. This is by design; agents reaching primer
over MCP can't sit through a human approval loop, so the tool
becomes inaccessible. To restore it: drop the policy.

## Mental model

A `ToolApprovalPolicy` row has:
- `toolset_id` and `tool_name` — the call to gate.
- `enabled` — flipping this to false bypasses the gate without
  deleting the row.
- `approval` — a discriminated union with `type` ∈ {`required`,
  `policy`, `llm`}, each carrying its own config.

The dispatch path is:

1. `ToolExecutionManager` is asked to run tool `T` with args `A`.
2. It calls `ApprovalResolver.find(toolset_id, T)`. Result is the
   policy row or None.
3. If None, dispatch.
4. If found and `enabled=False`, dispatch.
5. If found and `enabled=True`, evaluate the gate. Outcome is one of:
   - `allowed` — dispatch.
   - `required` — raise `YieldToWorker(Yielded(...))` with tool name
     `_approval` and resume metadata that holds the original tool +
     args. The session parks. An operator (or a channel-forwarded
     prompt; see [channels](channels.md)) responds. On resume with
     `decision=approve`, the worker re-dispatches with `bypass_approval=True`.
     On resume with `decision=reject`, the LLM gets a `ToolResultPart`
     saying "denied by operator".
   - `error` — any unexpected exception in the gate (policy failed
     to compile, LLM judge timed out, resolver couldn't read the
     row). Fail closed: treated as `required` with a reason string
     captured for the operator.

The fail-closed posture is deliberate. A misconfigured policy should
not silently allow calls through — the operator wants visibility.
Any uncertain outcome blocks until human review.

The approval state lives on the session's parked-status fields:
`parked_status="parked"`, `parked_tool_name="_approval"`,
`parked_state_blob` carries the LLM message buffer, and the resume
metadata embedded in the yield captures the original tool call. The
worker picks all this back up when the resume event arrives.

## Lifecycle and states

A policy itself has no lifecycle beyond `enabled / disabled`. The
*gate evaluation* has these outcomes per call:

- **allowed (dispatch).** Tool runs; result returns to the LLM.
- **required (parked).** Session is parked. Two terminal resolutions:
  - **approved** — the worker resumes with `bypass_approval=True`, the
    tool dispatches, the result returns.
  - **rejected** — the worker resumes with a `ToolResultPart` that
    says the call was rejected; the LLM continues with that as if it
    were a tool error.
- **timeout (rejected).** If the approval doesn't arrive within the
  policy's timeout (default: from the policy's `timeout_seconds`
  field; null = no timeout), the worker injects a
  `YieldTimeout`, the resume hook produces a synthetic rejection,
  and the LLM continues.
- **superseded (rejected).** If a new user turn arrives at the chat
  while an approval is pending, the pending approval is auto-rejected
  with reason "superseded by new user input" — the user's next
  message takes priority over the stalled approval.
- **cancelled (rejected).** Operator explicitly cancels the pending
  approval from the console. Same effect as rejected but with reason
  "cancelled by operator".

## MCP tools

This capability has no MCP tools of its own. Policy CRUD is
operator-only (REST routes under `/v1/tool_approval_policies` + the
console UI). Agents don't manage their own approval policies — by
design.

What an agent *does* see:

- A previously-available tool can disappear from the next
  `tools/list` response. (If MCP exposure is involved.)
- A tool call can return a `tool_rejected` result when an approval
  was set up while the agent was running. The output contains the
  reason string ("rejected by operator: <message>").

## Workflows

### Workflow 1 — operator gates `system::delete_collection` behind required approval

**Goal.** Make every collection deletion require a human nod.

1. Operator opens the Tool Approval Policies page in the console.
2. Clicks "New policy". Picks toolset `system`, tool `delete_collection`,
   approval type `required`, enabled.
3. The next time any agent (chat, session, or graph) calls
   `system::delete_collection`, the session parks and a prompt
   appears in the operator's pending-approvals queue.
4. Operator clicks approve. The worker resumes; the tool dispatches;
   the agent's next message includes the deletion result.
5. If the operator clicks reject, the agent receives a `tool_rejected`
   result and continues its reasoning with that information ("the
   operator said no — I should propose an alternative").

### Workflow 2 — operator wires up policy-based approval for HTTP requests to internal hosts

**Goal.** Auto-allow `web::http-request` to `https://*.example.com`
but require human review for any other host.

1. Operator picks toolset `web`, tool `http-request`, approval type
   `policy`. Provides a Rego rule like
   `allow { input.arguments.url =~ "^https://[a-z0-9-]+\\.example\\.com" }`.
2. The first call with `url=https://api.example.com/foo` is evaluated:
   Rego returns `allow=true`, the gate allows, dispatch proceeds
   immediately. No park.
3. The first call with `url=https://random.notmydomain.com/foo` is
   evaluated: Rego returns `allow=false`. The gate parks the session;
   operator gets a prompt with the URL highlighted.
4. The Rego compile is cached after first use. Editing the rule
   invalidates the cache; the next call recompiles. Compile failures
   fail closed (treated as `required`).

## Gotchas

- **The gate runs BEFORE the tool dispatches, not after.** Approval is
  a pre-check, not a post-validation. The tool's side effects are
  guaranteed not to have run when the gate parks the session — the
  operator's decision is the only thing that triggers them.
- **Fail closed on any error.** Policy compile failure, judge LLM
  timeout, resolver storage outage — all of these produce a
  `required` outcome with the original exception captured for the
  operator. The agent never silently slips through a broken gate.
- **A new user turn supersedes a pending approval.** If a chat is
  parked on approval and the user sends another message, the approval
  is auto-rejected with reason "superseded by new user input". The
  rationale: the user's later message implicitly redirects intent;
  honouring the old approval after the user already moved on creates
  surprising history rewriting.
- **MCP exposure silently hides approval-gated tools.** Adding a
  `required` policy to a tool that's in `mcp_exposure.allowed_tools`
  removes it from MCP `tools/list` until the policy is dropped.
  Operators with both surfaces in play need to remember this.
- **One policy per `(toolset_id, tool_name)`.** There's no wildcard
  tool name, no policy that applies to "every tool in this toolset",
  no precedence rules. If you want every tool in toolset `system` to
  require approval, you create one policy per tool.
- **Approval bypass is per-call, not per-session.** `bypass_approval=
  True` is set by the resume hook for the one call that was approved;
  subsequent calls to the same tool are gated again. Operators don't
  approve "this tool from now on" — they approve "this specific call".
- **The parked-tool name is literal `_approval`** in the parked-state
  fields. Code that introspects park state and dispatches on tool
  name treats `_approval` as a special case distinct from real tool
  yields.

## Related

- [yielding](yielding.md) — the underlying park/resume primitive.
  Approval reuses it; the resume metadata holds the original tool
  call.
- [mcp-exposure](mcp-exposure.md) — the silent-hide interaction with
  required-approval policies.
- [channels](channels.md) — channels can forward approval prompts to
  Slack/Telegram/Discord so the operator doesn't have to watch the
  console.
- [chats](chats.md) — the "new turn supersedes pending approval"
  semantics live in the chat-turn drain loop.
