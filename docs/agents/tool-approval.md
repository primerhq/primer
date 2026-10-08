---
slug: tool-approval
title: Tool approval policies
summary: Pre-dispatch gates that pause tool execution behind operator approval, policy evaluation, or LLM-judge calls.
related: [yielding, mcp-exposure, agents, sessions]
mcp_tools: []
---

# Tool approval policies

## Overview

A **tool approval policy** is a row that says "before this tool is
allowed to run, run a gate." It exists because some tool calls are
risky enough that the operator wants visibility - or veto power -
before they execute. The mechanism is a generalisation of an
"are-you-sure" prompt, with three kinds of gate: required (a human
must click approve), policy (evaluate a Rego rule against the call
arguments), and llm (ask a judge model whether to allow). All three
share a common shape: at LLM-call time, before the tool actually
dispatches, the gate runs. If it says block, the call yields and parks
like any yielding tool, in a session or a graph. If it says allow,
dispatch proceeds.

A policy is identified by `(toolset_id, tool_name)` - a wildcard tool
name is not supported in v1; one row per concrete tool. Policies are
optional: a tool with no policy row dispatches immediately. The
policy lookup runs on every call (it's not cached on the tool's
descriptor) so flipping a policy on takes effect for the next call
without restart.

The thing that catches everyone is the interaction with MCP. The MCP
exposure layer treats "this tool has a `required`-type policy" as
"this tool cannot be exposed to MCP clients at all" - because MCP
has no pause/resume primitive. So if you add an approval policy to a
tool that was previously available over MCP, it silently disappears
from external `tools/list`. This is by design; agents reaching primer
over MCP can't sit through a human approval loop, so the tool
becomes inaccessible. To restore it: drop the policy.

## Mental model

A `ToolApprovalPolicy` row has:
- `toolset_id` and `tool_name` - the call to gate.
- `enabled` - flipping this to false bypasses the gate without
  deleting the row.
- `approval` - a discriminated union with `type` ∈ {`required`,
  `policy`, `llm`}, each carrying its own config.
- `preview_args` - optional dotted paths into the tool's arguments that the approval card may show (see "What an approval card shows").

The dispatch path is:

1. `ToolExecutionManager` is asked to run tool `T` with args `A`.
2. It calls `ApprovalResolver.find(toolset_id, T)`. Result is the
   policy row or None.
3. If None, dispatch.
4. If found and `enabled=False`, dispatch.
5. If found and `enabled=True`, evaluate the gate. Outcome is one of:
   - `allowed` - dispatch.
   - `required` - raise `YieldToWorker(Yielded(...))` with tool name
     `_approval` and resume metadata that holds the original tool +
     args. The session parks. An operator (or a channel-forwarded
     prompt; see [channels](channels.md)) responds. On resume with
     `decision=approve`, the worker re-dispatches with `bypass_approval=True`.
     On resume with `decision=reject`, the LLM gets a `ToolResultPart`
     saying "denied by operator".
   - `error` - any unexpected exception in the gate (policy failed
     to compile, LLM judge timed out, resolver couldn't read the
     row). Fail closed: treated as `required` with a reason string
     captured for the operator.

The fail-closed posture is deliberate. A misconfigured policy should
not silently allow calls through - the operator wants visibility.
Any uncertain outcome blocks until human review.

The approval state lives on the session's parked-status fields:
`parked_status="parked"`, `parked_tool_name="_approval"`,
`parked_state_blob` carries the LLM message buffer, and the resume
metadata embedded in the yield captures the original tool call. The
worker picks all this back up when the resume event arrives.

## What an approval card shows

The Inbox card of a parked approval (the phone's Inbox tab, and the rail line on the desktop) draws the tool and a one-line preview of its arguments. The full call
is never hidden from whoever may decide it ("Show all", and the session's own pending-yields route, return it whole); the card is the part drawn on a screen
without anyone asking, so it draws only what is declared safe to draw:

- **The tool declares it.** `make_tool(preview_args=("path", "mode"))` (in memory only, never serialized) lists dotted paths into the arguments; a path allows
  its whole subtree and a list is transparent (`entity.nodes.agent_id` is the `agent_id` of every node). A path that names nothing is an error when the tool is
  built. The nine tools the platform gates by default (the builder's `crud` toolset: create and update of agents, graphs and triggers, and the Python toolset
  tools) declare the paths that say WHAT is created or changed and leave out the free text (an agent's system prompt, a graph node's templates, a webhook
  trigger's token and HMAC secret, a Python toolset's source).
- **The operator can override it.** `ToolApprovalPolicy.preview_args` (a list of the same paths) wins over the tool's declaration, and `[]` shows no value. This is the
  only way to cover an MCP tool or a Python tool, whose author declared nothing. Each path must name an argument of the gated tool: the console and the system
  `create_` / `update_tool_approval_policy` tools refuse a path that does not (a typo would hide more than meant, silently), and refuse paths for a tool that is not in
  the catalogue right now (they cannot be checked).
- **With neither, a default.** Only an argument whose schema is a closed set (a boolean, an integer, a number, `null`, an enum or a const) is shown; text, objects and
  lists are not, and a schema that cannot be proven closed counts as text.
- **Resolved at park time.** The tool manager resolves the effective list (policy, else tool, else default) when it parks the call and stamps it into the park as
  `preview: {paths, source}`. A park from before the field, or a graph park without a stamp, takes the default rule by the value's type (a boolean, a number or `null`
  is shown, the rest is not).
- **What the card says.** An argument not allowed is drawn as its name and `<hidden>` and is never read (not stringified, not measured). `<redacted>` is a different
  word: it means the scrubber FOUND a secret in a value the allowlist let through (the scrubber still runs on every shown value). The row carries `hidden_keys` (what was
  withheld, as dotted paths) and `preview` (`policy`, `tool`, `default` or `unstamped`).
- **`call_tool`.** A policy on the tool `call_tool` runs is filtered by THAT tool's list; a policy on `call_tool` itself shows `toolset_id` and `tool_name` and the inner tool's
  paths, and withholds the arguments of an inner tool it cannot find.

## Lifecycle and states

A policy itself has no lifecycle beyond `enabled / disabled`. The
*gate evaluation* has these outcomes per call:

- **allowed (dispatch).** Tool runs; result returns to the LLM.
- **required (parked).** Session is parked. Two terminal resolutions:
  - **approved** - the worker resumes with `bypass_approval=True`, the
    tool dispatches, the result returns.
  - **rejected** - the worker resumes with a `ToolResultPart` that
    says the call was rejected; the LLM continues with that as if it
    were a tool error.
- **timeout (rejected).** If the approval doesn't arrive within the
  policy's timeout (default: from the policy's `timeout_seconds`
  field; null = no timeout), the worker injects a
  `YieldTimeout`, the resume hook produces a synthetic rejection,
  and the LLM continues.
- **not superseded by a new message.** A message sent to a session
  that is parked on an approval does NOT decide it: the session counts
  as busy, so the message is queued as the next turn and the approval
  stays pending until it is decided or times out. (Only a pending
  external-tool call is cancelled by a new message, with reason
  "superseded by new user message".)
- **cancelled (rejected).** Operator explicitly cancels the pending
  approval from the console. Same effect as rejected but with reason
  "cancelled by operator". Cancelling the yield is deciding the gate,
  so only a user the gate's approvers admit (or an admin) can do it;
  see "Who may decide a gate" under Gotchas.

## Pending-approval HTTP endpoints

Operators (and channel bridges) can query which approval is currently
waiting on a session:

```
GET /v1/sessions/{session_id}/tool_approval/pending
```

It returns 200 with this envelope when an approval is pending:

```json
{
  "tool_call_id": "tc-abc",
  "tool_name": "delete_workspace",
  "arguments": {"id": "ws-1"},
  "policy_id": "p-required",
  "approval_type": "required",
  "gate_reason": "destructive operation",
  "parked_at": "2026-06-14T10:00:00+00:00",
  "timeout_at": "2026-06-14T10:10:00+00:00"
}
```

Both return 404 `/errors/not-found` (RFC 7807) if the session does
not exist or is not currently waiting on an `_approval` gate. To respond
(a reply on a mapped platform thread resolves the same pending gate):

```
POST /v1/sessions/{session_id}/tool_approval/respond
Body: {"tool_call_id": "tc-abc", "decision": "approved"|"rejected", "reason": "..."}
```

## MCP tools

This capability has no MCP tools of its own. Policy CRUD is
operator-only (REST routes under `/v1/tool_approval_policies` + the
console UI). Agents don't manage their own approval policies - by
design.

What an agent *does* see:

- A previously-available tool can disappear from the next
  `tools/list` response. (If MCP exposure is involved.)
- A tool call can return a `tool_rejected` result when an approval
  was set up while the agent was running. The output contains the
  reason string ("rejected by operator: <message>").

## Workflows

### Workflow 1 - operator gates `system::delete_collection` behind required approval

**Goal.** Make every collection deletion require a human nod.

1. Operator opens the Tool Approval Policies page in the console.
2. Clicks "New policy". Picks toolset `system`, tool `delete_collection`,
   approval type `required`, enabled.
3. The next time any agent (session or graph) calls
   `system::delete_collection`, the session parks and a prompt
   appears in the operator's pending-approvals queue.
4. Operator clicks approve. The worker resumes; the tool dispatches;
   the agent's next message includes the deletion result.
5. If the operator clicks reject, the agent receives a `tool_rejected`
   result and continues its reasoning with that information ("the
   operator said no - I should propose an alternative").

### Workflow 2 - operator wires up policy-based approval for HTTP requests to internal hosts

**Goal.** Auto-allow `web__http_request` to `https://*.example.com`
but require human review for any other host.

1. Operator picks toolset `web`, tool `http_request`, approval type
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
  guaranteed not to have run when the gate parks the session - the
  operator's decision is the only thing that triggers them.
- **Fail closed on any error.** Policy compile failure, judge LLM
  timeout, resolver storage outage - all of these produce a
  `required` outcome with the original exception captured for the
  operator. The agent never silently slips through a broken gate.
- **A policy is validated when it is saved**, through the console, the
  REST route or the `system::create_tool_approval_policy` /
  `system::update_tool_approval_policy` tools, with the same rules. A
  second policy for the same `(toolset_id, tool_name)` returns
  `type=conflict` naming the existing one, Rego that does not compile
  returns `type=validation-error` on `approval.policy`, and an LLM judge
  must name a stored provider (`approval.provider_id`) and a model that
  provider publishes (`approval.model`). Nothing is stored on a refusal.
  Keep one policy per tool: the gate looks up a single policy for the
  tool, so a leftover duplicate would make which one applies depend on
  storage order.
- **Who may decide a gate is part of the gate.** A policy can name approvers (`approvers`: specific users, or roles; admins can
  always decide), and a Rego or judge verdict can route one call to others. The effective spec is recorded when the call parks,
  for every park (the agent loop and `call_tool` alike), and EVERY way of deciding it is checked against it: `POST
  .../tool_approval/respond`, `POST .../yields/{tool_call_id}/cancel` on an approval gate (a cancel is a rejection) and a reply
  from a channel. A user the spec does not admit gets `403 approver_mismatch`. The spec checked is the stamp of the specific gate the `tool_call_id` names, also on a graph park (where the session's first gate is not necessarily the one named); a cancel of a parked approval whose gate cannot be found is admin-only. Only an approval is judged: cancelling an `ask_user` question or an external-tool wait (also on a graph park, whose top-level yield always reads as an approval) is open to whoever could cancel it before. **A session owner who is not an approver can therefore no longer cancel their own approval yield** (cancelling the whole session still works for them). **A channel reply is refused on a gate restricted
  to specific approvers**: a Slack, Discord or Telegram user is not an identified primer user, so such a gate is decided in the
  console by an approver or an admin; a gate with no restriction can still be decided from the channel. A spec that cannot be
  read is admin-only, and so is a `call_tool` gate that was parked before approvers were recorded. When several enabled policies
  exist for one tool, the gate trips unconditionally and only an admin decides it until the extra row is deleted. A channel reply is also refused when the park names an approval gate but no pending entry carries its approver spec: a pending ToolCall whose tool is neither `_approval` nor a registered value-yielding tool (it is sent to the channel as an approval prompt, but is not an approval gate), a checkpoint that lists the gate only in its dispatch view, and a legacy park with no `tool_name` whose event key is the `tool_approval:<session>:<tool_call_id>` the reply would be published to.
- **A new user message does not supersede a pending approval.** If a
  session is parked on approval and the user sends another message,
  the message is queued behind the open turn and the approval stays
  pending (no auto-rejection). To abandon the gated call, cancel its
  yield (an approver or an admin) or cancel the session.
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
  approve "this tool from now on" - they approve "this specific call".
- **The parked-tool name is literal `_approval`** in the parked-state
  fields. Code that introspects park state and dispatches on tool
  name treats `_approval` as a special case distinct from real tool
  yields.

## Related

- [yielding](yielding.md) - the underlying park/resume primitive.
  Approval reuses it; the resume metadata holds the original tool
  call.
- [mcp-exposure](mcp-exposure.md) - the silent-hide interaction with
  required-approval policies.
- [channels](channels.md) - channels can forward approval prompts to
  Slack/Telegram/Discord so the operator doesn't have to watch the
  console.
  semantics live in the session dispatch drain loop.
