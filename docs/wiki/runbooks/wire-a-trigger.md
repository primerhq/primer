---
title: Wire a trigger
summary: Approve a fire source, bind its delivery, prove an actual worker answer, then disable or remove only the owned test rows.
audience: user
tools: [trigger__create, trigger__update, trigger__delete, trigger__get, trigger__list, trigger__fire_now, trigger__create_subscription, trigger__get_subscription, trigger__list_subscriptions, trigger__update_subscription, trigger__delete_subscription, system__describe_tool, system__get_agent, workspaces__get_workspace, workspaces__list_workspace_sessions, workspaces__get_workspace_session]
related: [triggers/triggers, triggers/subscriptions, triggers/fire-now, runbooks/manage-a-workspace, runbooks/create-an-agent, runbooks/onboard-a-channel]
---
## Goal

Wire an owned trigger to a readable responder in a separate workspace, prove the actual session and input-derived answer, then leave the intended enabled state or remove the owned test setup.

## Preconditions

Confirm an existing non-default workspace you can write and a responder you may use. Create a missing prerequisite through [manage a workspace](runbooks/manage-a-workspace) or [create an agent](runbooks/create-an-agent); do not guess ids or alter the default workspace. Read [`workspaces__get_workspace`](reference/tools/workspaces/get_workspace) and [`system__get_agent`](reference/tools/system/get_agent).

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace", "arguments": {"id": "team-work"}}
```

```json
{"toolset_id": "system", "tool_name": "get_agent", "arguments": {"id": "team-helper"}}
```

Read live policy with [`system__describe_tool`](reference/tools/system/describe_tool) for every write. Create, update and delete of the trigger ship required gates; subscription writes and fire_now ship no gate. Policy can be retuned. These defaults are not permission to bypass a parked call through another surface.

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "trigger", "tool_name": "create"}}
```

## Roles and gates

All cited tools require the user role. The trigger's access and each target's access still apply after approval.

| operation | shipped gate | required authority |
|---|---|---|
| [`trigger__create`](reference/tools/trigger/create) | required | caller becomes the trigger's access owner and run-as saver |
| [`trigger__update`](reference/tools/trigger/update) | required | full on trigger and full on its subscription targets; an accepted edit records its saver as run-as owner |
| [`trigger__delete`](reference/tools/trigger/delete) | required | owner on trigger and accessible subscription targets; removes its subscriptions too |
| [`trigger__create_subscription`](reference/tools/trigger/create_subscription), [`trigger__update_subscription`](reference/tools/trigger/update_subscription), [`trigger__delete_subscription`](reference/tools/trigger/delete_subscription) | none | full on trigger and target workspace; a save stamps the caller as subscription owner |
| [`trigger__fire_now`](reference/tools/trigger/fire_now) | none | full on trigger and targets; runs at the saved owners' current role ceiling |

A required worker call parks once. Wait for the stamped approver's decision; do not retry it or switch to a twin. Rejection has no requested effect. Approval retains the original caller and cannot grant access the caller lacks. Native MCP may answer tool_unavailable for a required gate or role/exposure refusal. Read-only sharing does not permit writing a subscription or firing a trigger. An inaccessible trigger/target is answered as missing.

The approval card shows the exact create config or the named row and update/delete arguments, plus the unique gate occurrence. The person eligible under the live policy decides that occurrence; inspect both policy and arguments before deciding, rather than assuming any approval grants a permanent capability.

## Steps

1. Choose the source. The worked source is delayed with a confirmed far-future instant so the manual acceptance fire cannot race its clock. Replace the example time deliberately before a real scheduled use. Creating the source alone delivers nothing.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "team-reminder", "name": "Team reminder", "config": {"kind": "delayed", "fire_at": "2099-01-01T09:00:00Z"}}}
```

For a recurring source instead, use a valid cron, IANA timezone and explicit catchup policy. A kind cannot be changed later; create a different trigger. Delayed/scheduled configs reject interactive.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "team-weekday", "name": "Weekday reminder", "config": {"kind": "scheduled", "cron": "0 9 * * 1-5", "timezone": "Asia/Dubai", "catchup": "one"}}}
```

For a webhook source, omit token: the server mints it. The owner or an admin can read the real credential; another reader sees a mask. Keep the URL/token private, never reuse the mask, and provision an optional HMAC secret privately before giving an outside sender access. A webhook URL is a credential, not a logged-in user. See [webhook requests and signatures](triggers/fire-now). This runbook's manual delayed-source proof does not prove an external webhook sender or a clock tick.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "team-webhook", "name": "Team webhook", "config": {"kind": "webhook"}}}
```

Channel sources require the actual provider/room, event matcher and listener activation in [onboard a channel](runbooks/onboard-a-channel); do not use this non-channel subscription example as a channel binding.

2. Retain the returned opaque trigger id, not its slug. Reread it with [`trigger__get`](reference/tools/trigger/get), and check its config, enabled flag, owner and access. [`trigger__list`](reference/tools/trigger/list) lists only visible triggers.

```json
{"toolset_id": "trigger", "tool_name": "get", "arguments": {"id": "tr-42"}}
```

```json
{"toolset_id": "trigger", "tool_name": "list", "arguments": {"kind": "delayed"}}
```

3. Create the delivery initially disabled, inspect it, then enable it. The subscription write is ungated by default, but requires full on the trigger and target. The fresh session runs as the saved subscription owner, capped by the trigger owner's current role; approval never substitutes the approver's authority. The rendered fire context becomes its actual initial instructions.

```json
{"toolset_id": "trigger", "tool_name": "create_subscription", "arguments": {"trigger_id": "tr-42", "config": {"kind": "agent_fresh_session", "workspace_id": "team-work", "agent_id": "team-helper"}, "payload_template": "Report fire {{ fire_id }} from {{ trigger_id }}.", "parallelism": "queue", "enabled": false}}
```

Read the returned subscription id with [`trigger__get_subscription`](reference/tools/trigger/get_subscription) and compare it with [`trigger__list_subscriptions`](reference/tools/trigger/list_subscriptions).

```json
{"toolset_id": "trigger", "tool_name": "get_subscription", "arguments": {"trigger_id": "tr-42", "subscription_id": "sb-42"}}
```

```json
{"toolset_id": "trigger", "tool_name": "list_subscriptions", "arguments": {"trigger_id": "tr-42"}}
```

An enabled trigger with no enabled subscriptions can report a fire_id and zero deliveries. That is not a worker answer. Enable the exact inspected subscription with [`trigger__update_subscription`](reference/tools/trigger/update_subscription).

```json
{"toolset_id": "trigger", "tool_name": "update_subscription", "arguments": {"trigger_id": "tr-42", "subscription_id": "sb-42", "enabled": true}}
```

The subscription config is immutable: delete/recreate to change targets. Omitted update fields are retained; explicit null clears a nullable payload_template or description. queue allows a fresh session for every fire; skip suppresses a fresh run while its previous session is busy. Do not promise exactly-once delivery.

4. Make one deliberate real fire. This does not wait for the worker's answer; inspect every result's ok/skipped/error_code and follow the returned artefact_id. Failed or skipped dispatch is not successful execution.

```json
{"toolset_id": "trigger", "tool_name": "fire_now", "arguments": {"id": "tr-42"}}
```

## Verification

List the actual target sessions with [`workspaces__list_workspace_sessions`](reference/tools/workspaces/list_workspace_sessions) and read the returned session id with [`workspaces__get_workspace_session`](reference/tools/workspaces/get_workspace_session). list_workspace_sessions items and get_workspace_session.info are SessionInfo objects: their identifier is session_id; the get answer also has status. Require the same workspace/agent, saved ordinary initiator, actual rendered fire_id/trigger_id instructions, a completed turn and a real input-derived assistant answer. A metadata row or fire_id alone is not acceptance.

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_sessions", "arguments": {"workspace_id": "team-work", "limit": 20}}
```

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "team-work", "session_id": "sess-42"}}
```

Observe the actual transcript through [the session observation flow](sessions/observe-a-session). Reread the subscription: last_fired_at must be set and last_fire_error must be clear after the successful delivery. A read-only non-owner should still be refused writes, and another reader of a webhook trigger must receive a masked token while its owner/admin receives the real one.

For deliberate rollback, first disable the trigger with the required approval and verify fire_now returns skipped=true, fire_id=null, results=[] with no new session. Disabling retains subscriptions.

```json
{"toolset_id": "trigger", "tool_name": "update", "arguments": {"id": "tr-42", "enabled": false}}
```

Remove only the owned subscription and verify get/list no longer return it, then approve deletion of the exact owned trigger and check trigger__list no longer contains it. Preserve the default workspace and unrelated subscriptions.

```json
{"toolset_id": "trigger", "tool_name": "delete_subscription", "arguments": {"trigger_id": "tr-42", "subscription_id": "sb-42"}}
```

```json
{"toolset_id": "trigger", "tool_name": "delete", "arguments": {"id": "tr-42"}}
```

## Refusal and failure

- A rejected create/update/delete is terminal for that request. Do not resubmit while parked or route around the gate.
- Missing trigger/target, read-only write or an approved action outside the caller's access must leave the requested rows unchanged. Do not widen access just to make a diagnostic green.
- A subscription target is checked at save and fire. Deleted/disabled users or revoked/expired owner tokens cannot start fresh runs; owner_unresolvable and target_out_of_reach require a legitimate re-save/access repair, not an admin identity substituted into the fire.
- Webhook tokens are visible only to the owner/admin; read sharing and approval do not grant token visibility. Retain the current secret on a config replacement as documented; a masked token is not a credential. Rotation is also available through the generated `triggers__rotate_token` operation; it retains the same owner/admin token-visibility checks and is not a step promised by this sampled runbook.
- fire_now supplies neither webhook request fields nor ChannelEvent. Templates naming absent values fail payload_template_failed, and channel matchers can return skipped_no_match. Use the source-specific flow for those real inputs.
- A disabled trigger or subscription, busy skip, invalid template or failed worker is not completion. Read the exact error and actual session transcript before changing anything. Preserve unrelated rows and the default workspace on failure.

## Related

[Trigger kinds](triggers/triggers), [subscription ownership and targets](triggers/subscriptions), [fire diagnostics and webhooks](triggers/fire-now), [workspace setup](runbooks/manage-a-workspace), [agent setup](runbooks/create-an-agent), [channel onboarding](runbooks/onboard-a-channel).
