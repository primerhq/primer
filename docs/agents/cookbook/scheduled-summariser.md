---
slug: cookbook/scheduled-summariser
title: Scheduled Summariser
summary: Run an agent every weekday morning to summarise yesterday's logs from a workspace and post the result to a bound Slack channel.
mcp_tools:
  - system::create_agent
  - workspaces::create_workspace_template
  - workspaces::create_workspace
  - trigger::create
  - trigger::create_subscription
  - trigger::fire_now
  - workspaces::list_workspace_sessions
  - workspaces::get_workspace_session
---

## Goal
Every weekday at 9 AM local time, a scheduled trigger starts a fresh agent session. The agent reads yesterday's log files from a sandbox workspace, summarises them, and posts the summary to a bound Slack channel. An `ask_user` prompt then waits for an operator approval before the session closes.

## Prerequisites
- An LLM provider id for the summariser agent.
- A workspace provider id; the template mounts or has access to the log directory and a TTL of at least 60 minutes.
- A Slack channel provider already configured, with a channel bound to the workspace via an association that has **Forward ask_user** enabled (so the agent's `ask_user` prompt is delivered to Slack).

## Steps
### 1. Create the summariser agent
`system::create_agent`
```json
{
  "entity": {
    "id": "log-summariser",
    "description": "Summarises yesterday's logs and posts to Slack",
    "system_prompt": ["Read yesterday's log files from the workspace, summarise the notable events, post the summary to the bound channel, then ask_user to approve before finishing."],
    "model": { "provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6" }
  }
}
```
Response:
```json
{ "id": "log-summariser" }
```
Tune the prompt against real log volume before scheduling; a noisy first production run is hard to diagnose after the fact.

### 2. Create the workspace template
`workspaces::create_workspace_template`
```json
{
  "entity": {
    "id": "log-summary-template",
    "description": "log dir access, 60m TTL",
    "provider_id": "local-1",
    "backend": { "kind": "local" }
  }
}
```
Response:
```json
{ "id": "log-summary-template" }
```

### 3. Materialise the workspace
`workspaces::create_workspace`
```json
{ "template_id": "log-summary-template" }
```
Response:
```json
{ "id": "ws-1", "phase": "running" }
```
Thread `id` ("ws-1") into the subscription. Wait until `phase` is `running`.

### 4. Create the scheduled trigger
`trigger::create`
```json
{
  "slug": "weekday-summary",
  "name": "Weekday summary",
  "config": { "kind": "scheduled", "cron": "0 5 * * 1-5", "timezone": "Asia/Dubai", "catchup": "one" },
  "enabled": true
}
```
Response:
```json
{ "id": "trg-1", "slug": "weekday-summary" }
```
The cron is evaluated in the supplied `timezone`; `0 5 * * 1-5` at `Asia/Dubai` is 9:00 AM on weekdays. `catchup: "one"` fires a single missed tick once on recovery from downtime.

### 5. Attach an agent_fresh_session subscription
`trigger::create_subscription`
```json
{
  "trigger_id": "trg-1",
  "config": {
    "kind": "agent_fresh_session",
    "agent_id": "log-summariser",
    "workspace_id": "ws-1"
  }
}
```
Response:
```json
{ "id": "sub-1", "trigger_id": "trg-1" }
```
Set `parallelism` to `skip` (via `update_subscription`) so a slow run does not stack on itself.

### 6. Fire now to verify
`trigger::fire_now`
```json
{ "id": "trg-1" }
```
Response:
```json
{ "fire_id": "fire-1", "results": [ { "subscription_id": "sub-1", "session_id": "ses-1" } ] }
```

### 7. Watch the run and approve
`workspaces::list_workspace_sessions`
```json
{ "workspace_id": "ws-1" }
```
Response:
```json
{ "items": [ { "id": "ses-1", "status": "running" } ] }
```
Then poll:

`workspaces::get_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "waiting" }
```
When the agent finishes the summary it issues `ask_user`; with **Forward ask_user** enabled the prompt is delivered to the bound Slack channel as an Approve / Reject message. After the operator clicks Approve, the session ends:
```json
{ "id": "ses-1", "status": "ended", "ended_reason": "completed" }
```
Reject sends the rejection reason back to the agent so it can revise.

## Verify
The fired session reaches `status: "waiting"` on the `ask_user` prompt, the summary lands in the Slack channel, and after Approve the session ends with `ended_reason: "completed"`. After a clean approval the weekday cron runs unattended.

## Gotchas
- Workspace TTL must outlast the agent's longest turn. Default 30 minutes is usually fine; bump to 60 if log volume is large.
- The Slack channel provider needs the `chat:write` and `chat:read` scopes; the OAuth flow surfaces this during provider setup.
- Cron is evaluated in the trigger's `timezone`. Set it explicitly rather than relying on a default, and double-check the IANA value.
- The `ask_user` prompt only reaches Slack if the workspace-channel association has **Forward ask_user** enabled.

## Related
- `triggers`, `agents`, `sessions`, `channels`, `workspaces`
- `cookbook/create-and-run-a-session`
- `cookbook/pr-reviewer-on-cron`
- `cookbook/daily-incident-digest`
