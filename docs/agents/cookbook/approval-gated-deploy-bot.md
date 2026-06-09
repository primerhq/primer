---
slug: cookbook/approval-gated-deploy-bot
title: Approval-gated Deploy Bot
summary: A Slack-driven deploy agent whose deploy_prod tool is gated by a required approval policy, so the deploy step parks until a human approves and only runs after explicit operator approval.
mcp_tools:
  - system::create_channel_provider
  - system::create_channel
  - system::create_agent
  - system::create_workspace_channel_association
  - system::create_tool_approval_policy
  - workspaces::create_workspace
  - workspaces::create_workspace_session
  - workspaces::get_workspace_session
---

## Goal
An operator messages a Slack channel with a deploy command. The agent plans the deploy, then its `deploy_prod` call parks the session until a human approves. The deploy runs only after explicit approval; a rejection returns a clean error to the agent.

## Prerequisites
- A Slack app with App (`xapp-...`) and Bot (`xoxb-...`) tokens.
- A `deploy-tools` toolset registered with a `deploy_prod` tool and a `channels` toolset for posting results back.
- An LLM provider configured and a workspace template available.

## Steps
### 1. Create the Slack channel provider
`system::create_channel_provider`
```json
{
  "entity": {
    "id": "slack-deploys",
    "provider": "slack",
    "config": { "app_token": "xapp-REPLACE", "bot_token": "xoxb-REPLACE" }
  }
}
```
Response:
```json
{ "id": "slack-deploys" }
```

### 2. Create the channel and workspace
`system::create_channel`
```json
{
  "entity": {
    "id": "deploys",
    "provider_id": "slack-deploys",
    "external_id": "C0999DEPLOY",
    "label": "#deploys"
  }
}
```
Response:
```json
{ "id": "deploys" }
```
`workspaces::create_workspace`
```json
{ "template_id": "py-base" }
```
Response:
```json
{ "id": "ws-1", "phase": "running" }
```
Wait until `phase` is `running`.

### 3. Create the deploy agent
`system::create_agent`
```json
{
  "entity": {
    "id": "deploy-bot",
    "description": "Plans a deploy then calls deploy_prod, posting the outcome back to the channel",
    "model": { "provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6" },
    "tools": ["deploy-tools__deploy_prod", "channels__post_message"],
    "system_prompt": ["You are a deploy coordinator. Given a deploy target, produce a concise plan (services, migrations, cache flushes), then call deploy_prod with the target. After it resolves, post the outcome back to the originating channel. If the call is rejected, report the rejection and end cleanly."]
  }
}
```
Response:
```json
{ "id": "deploy-bot" }
```
Tell the agent in its prompt to handle a rejection gracefully so a rejected deploy ends the session cleanly rather than stalling.

### 4. Bind the channel to the workspace
`system::create_workspace_channel_association`
```json
{
  "entity": {
    "id": "wca-deploys",
    "workspace_id": "ws-1",
    "channel_id": "deploys",
    "enabled": true,
    "forward_ask_user": false,
    "forward_tool_approval": true
  }
}
```
Response:
```json
{ "id": "wca-deploys" }
```
`forward_tool_approval: true` surfaces the parked `deploy_prod` approval in Slack as well as the console.

### 5. Create the required approval policy on deploy_prod
`system::create_tool_approval_policy`
```json
{
  "entity": {
    "id": "gate-deploy-prod",
    "toolset_id": "deploy-tools",
    "tool_name": "deploy_prod",
    "approval": { "type": "required" },
    "timeout_seconds": 900
  }
}
```
Response:
```json
{ "id": "gate-deploy-prod" }
```
`type: "required"` parks every `deploy_prod` call for a manual decision. A parked session held past `timeout_seconds` (900s) auto-rejects rather than holding a worker slot.

### 6. Start a deploy session and watch it park
`workspaces::create_workspace_session`
```json
{
  "workspace_id": "ws-1",
  "binding": { "kind": "agent", "agent_id": "deploy-bot" },
  "initial_instructions": "Deploy the payments service to prod.",
  "auto_start": true
}
```
Response:
```json
{ "id": "ses-1", "status": "running" }
```
Poll until it parks on the gated call:

`workspaces::get_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "waiting" }
```
`status: "waiting"` means the session is parked on the `deploy_prod` approval. In production the inbound Slack message drives the session instead of `create_workspace_session`; the association routes it.

## Verify
The session reaches `status: "waiting"` on the `deploy_prod` call and does not deploy until approved. There is no MCP approve tool: an operator approves or rejects from the console Approvals > Pending tab or via the REST respond endpoint. Approving resumes the session and dispatches `deploy_prod`; rejecting returns a clean error the agent reports back to `#deploys`.

## Gotchas
- Approving or rejecting a parked deploy is an operator/REST action, not an MCP call. The MCP tools here create the gate and observe the parked session.
- `deploy_prod` is irreversible. Test the gate-pause path in a development session before enabling the policy in production.
- A parked session holds a worker slot until decided. Rely on `timeout_seconds` to auto-reject stale calls and page on-call separately.
- In a high-traffic Slack channel the forwarded approval prompt can scroll away. Pin approvals to a dedicated moderator channel via a second association with `forward_tool_approval: true` and `forward_ask_user: false`.

## Related
- `agents`, `sessions`, `channels`, `tool-approval`
- `cookbook/discord-moderation-helper`
- `cookbook/slack-question-answerer`
- `cookbook/create-and-run-a-session`
