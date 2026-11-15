---
title: "Approval identity, previews and stale decisions"
summary: "Distinguish the execution principal, the human decider, safe card previews and one occurrence of a gate."
order: 2
related: [approvals/policies, runbooks/moderate-a-request, sessions/observe-a-session]
---

## What it is

An approval authorizes one parked call. It does not lend the decider's permissions to the execution principal. The card preview is a bounded, scrubbed view of that call; it is not the whole call and is not evidence that hidden arguments are safe.

## Concepts

- Policy and judge input includes `toolset_id`, `tool_name`, `arguments`, `agent_id`, `session_id`, `requested_at` and `identity`. The identity distinguishes the person behind the run from the innermost agent, graph, user or system actor. A model cannot select another principal through a call argument.
- A tool approval records its decider for audit while the resumed operation keeps the parked execution person. Access and role checks still apply when execution resumes. Answering ask_user instead projects the authorized responder onto the resumed turn; these are different decisions.
- `gate_id` identifies one occurrence. Raw tool call ids can repeat across rounds, and graph siblings can use the same raw id. Always send the current gate_id with the selected pending tool_call_id, rather than reusing an old card's decision.
- Accepted decisions stop being pending before the worker resumes. A stale occurrence answers `approval_stale`; an occurrence no longer pending answers `gate_not_pending`. Reload the pending list and do not reinterpret a plain missing-session error as a replacement gate. An absent gate token cannot distinguish repeated occurrences, even where old clients are still accepted.

## How to

1. Read the actual pending item and its full call, identity, gate_id and approver rule. Workspace full access and being an allowed approver are separate requirements.
2. Inspect the card's hidden or redacted arguments. Use Show all or open the session when the summary is partial; never approve a blind partial card merely because a tool or policy declared a preview list.
3. Submit one intentional decision for that exact occurrence. Preserve a refusal or stale response; fetch current state before retrying.
4. Verify the pending occurrence disappears and then inspect the resumed operation's actual outcome. A 202 decision response is not evidence that execution succeeded.

## Tool calls

The [approval operation references](approvals) include pending lists, response routes and audit records. They reach the existing authorized response behavior; an agent cannot approve its own gate or turn a peer message into a decision. A direct MCP target cannot park for a required approval, so discovering it does not create a remote approval channel.

## Preview rules

- The policy's `preview_args` overrides the tool declaration; an empty list shows no values. Otherwise a declaration allows dotted argument paths and their subtrees. With neither declaration the default reveals only provably closed-set arguments, checked again against the actual value because the gate precedes validation.
- A withheld argument appears as `<hidden>` and its value is not read or measured. `<redacted>` means the scrubber recognized a credential name or shape. Declared text, nested headers, env values and free-form descriptions can contain secrets; a declared list is not trusted.
- `preview_lead` selects decision-leading fields. Their park-time fingerprints and changes let a card show a policy toggle, source or harness override without exposing withheld values. A fingerprint is not the omitted value.
- Path traversal and preview size are bounded. Ambiguous dictionary keys are not interpreted as nested paths; name/value pairs cannot reveal their value when the identifying name was withheld. Hidden tails count as hidden input.
- A call_tool wrapper uses the inner tool's declaration for its arguments; an unknown inner tool cannot grant a wider preview. The same role and execution identity checks apply to the inner dispatch.
- On a phone, Hidden on this card lists what was withheld. Show all must be associated with this exact current park before enabling a decision; cached full arguments from an earlier occurrence are not reusable.

## Gotchas

- Approver mismatch and opaque workspace-access refusal are different checks. No approved result authorizes an operation the execution person cannot perform.
- A steer, channel message or another session's note is not a response to a gate. A restricted approver policy can require the authenticated console even when replies are otherwise bound to a channel.
- Unknown arguments, source text and credential-like keys remain untrusted even when a preview declaration lets a subtree through.

## Related

[Approval policies](approvals/policies), [moderate a request](runbooks/moderate-a-request), [observe a session](sessions/observe-a-session), [sharing and access](start-here/sharing-and-access).
