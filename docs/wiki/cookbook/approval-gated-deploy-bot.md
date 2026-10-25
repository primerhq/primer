---
title: "Approval-gated deploy bot"
summary: "A deploy agent whose deploy tool is gated: the turn pauses until a person approves in the console or in Slack."
audience: admin
order: 12
tools: [system__create_channel_provider, system__create_channel, workspaces__create_workspace, system__create_agent, system__set_reply_binding, system__create_tool_approval_policy, workspaces__create_workspace_session, workspaces__get_workspace_session]
related: [approvals/policies, channels/reply-binding]
---
## Goal

A deploy agent whose `deploy_prod` call is gated. The turn plans the deploy, then the gated call parks the session until a person approves in the console or in Slack. The deploy runs only after the approval; a rejection returns a clean error the agent reports.

## Prerequisites

- A Slack app with both tokens: the App token (starts with `xapp-`) and the Bot token (starts with `xoxb-`).
- A `deploy-tools` toolset registered with a `deploy_prod` tool.
- A model profile the agent runs on, e.g. `anthropic-1--claude-sonnet-4-6`, and a workspace template.
- An admin: creating the provider and the approval policy need the admin role. The other tools here need the user role and are not approval-gated.

## Steps

### 1. Create the Slack provider
`system__create_channel_provider` stores the app's tokens. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_channel_provider", "arguments": {"entity": {"id": "slack-deploys", "provider": "slack", "config": {"app_token": "xapp-REPLACE", "bot_token": "xoxb-REPLACE"}}}}
```

Response (the stored provider, tokens masked):

```json
{"id": "slack-deploys", "provider": "slack", "config": {"app_token": "**********", "bot_token": "**********"}}
```

Thread the `id` into the channel below.

### 2. Create the channel
`system__create_channel` names the Slack channel the workspace posts to.

```json
{"toolset_id": "system", "tool_name": "create_channel", "arguments": {"entity": {"id": "deploys", "provider_id": "slack-deploys", "provider": "slack", "external_id": "C0999DEPLOY", "label": "#deploys", "config": {"chats": {"enabled": true}}}}}
```

Response:

```json
{"id": "deploys", "provider_id": "slack-deploys", "provider": "slack", "external_id": "C0999DEPLOY", "label": "#deploys"}
```

`external_id` is the Slack channel id. Thread the channel `id` into the reply binding below.

### 3. Create the workspace
`workspaces__create_workspace` materialises the workspace the deploy sessions run in.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"template_id": "py-base"}}
```

Response:

```json
{"id": "ws-9f2c", "template_id": "py-base", "phase": "pending"}
```

Thread the workspace `id` into the steps below.

### 4. Create the deploy agent
`system__create_agent` stores the deploy coordinator.

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "deploy-bot", "description": "Plans a deploy then calls deploy_prod, reporting the outcome", "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "tools": ["deploy-tools__deploy_prod"], "system_prompt": ["You are a deploy coordinator. Given a deploy target, produce a concise plan (services, migrations, cache flushes), then call deploy_prod with the target. End the turn by stating the outcome, or the rejection, plainly."]}}}
```

Response:

```json
{"id": "deploy-bot", "description": "Plans a deploy then calls deploy_prod, reporting the outcome"}
```

The agent needs no posting tool: the workspace's reply binding posts its questions, approvals and final answer to Slack. Tell it in the prompt to report a rejection cleanly so a refused deploy ends the session instead of stalling it.

### 5. Bind the workspace to the channel
`system__set_reply_binding` makes the workspace's sessions reach people through the channel.

```json
{"toolset_id": "system", "tool_name": "set_reply_binding", "arguments": {"workspace_id": "ws-9f2c", "channel_id": "deploys"}}
```

Response:

```json
{"ok": true, "workspace_id": "ws-9f2c", "channel_id": "deploys", "anchor": null}
```

Every session in the workspace posts its questions, approval requests, updates and answers to the channel; on Slack each session opens its own thread with its first post. This is a shared anchor: the sessions post into the channel, no thread-to-session mapping is written, and a plain reply typed in the channel steers no session.

### 6. Gate deploy_prod behind a person
`system__create_tool_approval_policy` parks every `deploy_prod` call for a manual decision. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_tool_approval_policy", "arguments": {"entity": {"id": "gate-deploy-prod", "toolset_id": "deploy-tools", "tool_name": "deploy_prod", "approval": {"type": "required"}, "timeout_seconds": 900}}}
```

Response (the stored policy):

```json
{"id": "gate-deploy-prod", "toolset_id": "deploy-tools", "tool_name": "deploy_prod", "approval": {"type": "required"}}
```

A call parked past `timeout_seconds` (900 s here) auto-rejects instead of holding a worker slot. Because the policy names no approvers, a person may decide from the console Inbox or from the Slack approval buttons in the session's thread; a policy that names approvers can be decided in the console only.

### 7. Start a deploy session
`workspaces__create_workspace_session` binds the agent and starts its first turn.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "binding": {"kind": "agent", "agent_id": "deploy-bot"}, "initial_instructions": "Deploy the payments service to prod.", "auto_start": true}}
```

Response:

```json
{"id": "sess-a1b2", "workspace_id": "ws-9f2c", "status": "running"}
```

### 8. Watch it park
Poll the session with `workspaces__get_workspace_session` until it parks on the gated call:

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-a1b2"}}
```

Response while it parks:

```json
{"info": {"session_id": "sess-a1b2", "status": "running"}, "status": "running"}
```

The session reads `status: "running"` while parked, and its full row's `parked_event_key` starts with `tool_approval:` (read the row with `workspaces__find_sessions`, [find sessions](sessions/find-sessions)). The approval request sits in `#deploys` with Approve and Reject buttons, headed by the workspace and session. Approving resumes the turn and dispatches `deploy_prod`; rejecting returns a clean error the agent reports as its final answer.

## Verify

The session parks on the `deploy_prod` call and does not deploy until a person approves. While it waits there is no other post: the binding posts no start message. Approving finishes the turn and posts the outcome to the session's thread in `#deploys`; rejecting posts the rejection instead.

## Gotchas

- There is no start message and no per-turn acknowledgement: the session posts its questions, its approvals and its final answer, and nothing else.
- In production the inbound Slack message drives the session instead of `workspaces__create_workspace_session`: a `channel` trigger and a `system__create_channel_binding` on it ([inbound bindings](channels/bindings)) start the deploy session, and the reply binding above keeps forwarding its gates.
- A parked session holds a worker slot until it is decided. Rely on `timeout_seconds` to auto-reject stale calls and page on-call separately.
- `deploy_prod` is irreversible. Test the gate-pause path in a development workspace before the policy guards production.
- A workspace has one standing reply binding; setting it again replaces it. Point it at a dedicated channel so the forwarded approval prompts do not scroll away under traffic.
- Approving or rejecting is a person's act (console Inbox or the channel buttons), not a tool call. No tool here approves.

## Related

- [Approval policies](approvals/policies)
- [Reply binding](channels/reply-binding)
- [Discord moderation helper](cookbook/discord-moderation-helper)
- [Monitor and resume a parked session](cookbook/monitor-and-resume-a-parked-session)
