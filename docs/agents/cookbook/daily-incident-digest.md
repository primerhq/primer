---
slug: cookbook/daily-incident-digest
title: Daily Incident Digest
summary: Every weekday morning, fire an agent that pulls overnight incidents from an indexed knowledge collection and posts a tagged digest to a Slack channel.
mcp_tools:
  - system::create_collection
  - system::create_agent
  - system::create_channel
  - trigger::create
  - trigger::create_subscription
  - trigger::fire_now
  - workspaces::list_workspace_sessions
  - workspaces::get_workspace_session
---

## Goal
Every weekday at 09:00 local time, a scheduled trigger starts a fresh agent session. The agent pulls overnight incident write-ups from an indexed knowledge collection, groups them by severity, and posts a tagged digest to the ops Slack channel.

## Prerequisites
- A Slack channel provider configured, with a channel bound to the target workspace.
- An embedding provider id and a vector search provider id for the collection.
- An LLM provider id for the agent.
- A materialised workspace (see `cookbook/create-and-run-a-session`) whose id is threaded into the subscription.

## Steps
### 1. Create the knowledge collection
`system::create_collection`
```json
{
  "entity": {
    "id": "incidents",
    "description": "Overnight incident write-ups",
    "embedder": { "provider_id": "hf-1", "model": "all-MiniLM-L6-v2" },
    "search_provider_id": "ssp-1"
  }
}
```
Response:
```json
{ "id": "incidents" }
```
Populate the collection with incident documents before the first fire. Each document should carry at least `severity` (e.g. `SEV-1`) and `started_at` (ISO timestamp) in its metadata so the agent can filter by recency. If the collection is empty on first fire the agent posts "nothing overnight" every morning until documents are added.

### 2. Create the digest agent
`system::create_agent`
```json
{
  "entity": {
    "id": "incident-digest-bot",
    "description": "Posts a severity-grouped overnight incident digest",
    "system_prompt": ["Search the incidents collection for items where started_at is after 22:00 yesterday. Group results by severity and post a plain-text digest to the Slack channel."],
    "model": { "provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6" }
  }
}
```
Response:
```json
{ "id": "incident-digest-bot" }
```
Enable the system toolset tools that cover knowledge search so the agent can query the collection.

### 3. Ensure the Slack channel exists
`system::create_channel`
```json
{
  "entity": {
    "id": "ops-incidents",
    "provider_id": "slack-1",
    "external_id": "C12345"
  }
}
```
Response:
```json
{ "id": "ops-incidents" }
```
Bind this channel to the workspace so the agent's posts land in Slack.

### 4. Create the scheduled trigger
`trigger::create`
```json
{
  "slug": "incident-digest-weekday",
  "name": "Incident digest weekday",
  "config": { "kind": "scheduled", "cron": "0 5 * * 1-5", "timezone": "Asia/Dubai", "catchup": "one" },
  "enabled": true
}
```
Response:
```json
{ "id": "trg-1", "slug": "incident-digest-weekday" }
```
Cron is evaluated in the supplied `timezone`; `0 5 * * 1-5` at `Asia/Dubai` is 09:00 on weekdays. `catchup: "one"` fires a single missed tick on recovery. Verify the next-fire time before relying on it.

### 5. Attach an agent_fresh_session subscription
`trigger::create_subscription`
```json
{
  "trigger_id": "trg-1",
  "config": {
    "kind": "agent_fresh_session",
    "agent_id": "incident-digest-bot",
    "workspace_id": "ws-1"
  }
}
```
Response:
```json
{ "id": "sub-1", "trigger_id": "trg-1" }
```
Optionally set a `payload_template` that injects `{{ fired_at }}` so the agent knows the exact fire time. Set `parallelism` to `skip` (via `update_subscription`) so a slow run does not stack.

### 6. Fire now to verify
`trigger::fire_now`
```json
{ "id": "trg-1" }
```
Response:
```json
{ "fire_id": "fire-1", "results": [ { "subscription_id": "sub-1", "session_id": "ses-1" } ] }
```

### 7. Watch the run
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
{ "id": "ses-1", "status": "ended", "ended_reason": "completed" }
```

## Verify
The fired session ends with `ended_reason: "completed"`; its transcript shows the agent searching the collection, grouping by severity, and posting the digest. The Slack channel's first post lists something like "Overnight: 2 SEV-1, 4 SEV-2, 11 noise" with links to the full write-ups.

## Gotchas
- The trigger schedule is immutable. For weekend coverage either use `0 5 * * *` from the start, or create a second trigger with `0 5 * * 0,6` and the same subscription.
- The semantic query is natural-language; recency filtering uses the collection's `started_at` metadata, not the query text. Ensure documents land with accurate timestamps at ingest time.
- Populate the collection before the first fire, or every morning's digest reports nothing overnight.

## Related
- `triggers`, `channels`, `agents`, `collections`, `sessions`
- `cookbook/create-and-run-a-session`
- `cookbook/pr-reviewer-on-cron`
- `cookbook/scheduled-summariser`
