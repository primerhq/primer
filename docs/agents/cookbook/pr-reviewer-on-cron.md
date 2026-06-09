---
slug: cookbook/pr-reviewer-on-cron
title: PR Reviewer On Cron
summary: Wire a scheduled trigger that fires an agent every hour to review new GitHub PRs in a git-enabled workspace and post comments back.
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
Stand up an hourly cron trigger whose subscription starts a fresh agent session in a git-enabled workspace. The agent lists new pull requests via a GitHub MCP connector, reviews each diff, and posts review comments back to GitHub.

## Prerequisites
- A GitHub MCP server configured and reachable, exposed to the agent via an enabled toolset.
- A workspace provider id to materialise workspaces from; the template's `init_commands` clone the target repo and install `git`.
- Permission to create agents, workspaces, and triggers over MCP.

## Steps
### 1. Create the review agent
`system::create_agent`
```json
{
  "entity": {
    "id": "pr-reviewer",
    "description": "Reviews open GitHub PRs and posts comments",
    "system_prompt": ["List open pull requests, review each file diff, and post review comments via the GitHub MCP tools. Keep a consistent review tone across runs."],
    "model": { "provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6" }
  }
}
```
Response:
```json
{ "id": "pr-reviewer" }
```
Pin the review tone in the system prompt so hourly runs read consistently to PR authors.

### 2. Create a git-enabled workspace template
`workspaces::create_workspace_template`
```json
{
  "entity": {
    "id": "pr-review-template",
    "description": "git + target repo cloned, 60m TTL",
    "provider_id": "local-1",
    "backend": { "kind": "local" },
    "init_commands": ["git clone https://github.com/acme/app.git repo"]
  }
}
```
Response:
```json
{ "id": "pr-review-template" }
```
Give the template a TTL of at least 60 minutes; a batch of PRs can take a while to review.

### 3. Materialise the workspace
`workspaces::create_workspace`
```json
{ "template_id": "pr-review-template" }
```
Response:
```json
{ "id": "ws-1", "phase": "running" }
```
Thread `id` ("ws-1") into the subscription config. Wait until `phase` is `running`.

### 4. Create the scheduled trigger
`trigger::create`
```json
{
  "slug": "pr-review-hourly",
  "name": "PR review hourly",
  "config": { "kind": "scheduled", "cron": "0 * * * *", "catchup": "skip" },
  "enabled": true
}
```
Response:
```json
{ "id": "trg-1", "slug": "pr-review-hourly" }
```
`0 * * * *` fires at the top of every UTC hour. `catchup: "skip"` stops missed ticks during downtime causing a burst of review runs. Thread `id` ("trg-1") into the subscription.

### 5. Attach an agent_fresh_session subscription
`trigger::create_subscription`
```json
{
  "trigger_id": "trg-1",
  "config": {
    "kind": "agent_fresh_session",
    "agent_id": "pr-reviewer",
    "workspace_id": "ws-1"
  }
}
```
Response:
```json
{ "id": "sub-1", "trigger_id": "trg-1" }
```
Each tick starts a brand-new session bound to the agent in this workspace. Set `parallelism` to `skip` (via `update_subscription`) if a review run may still be in flight when the next tick arrives.

### 6. Test with Fire now
`trigger::fire_now`
```json
{ "id": "trg-1" }
```
Response:
```json
{ "fire_id": "fire-1", "results": [ { "subscription_id": "sub-1", "session_id": "ses-1" } ] }
```
`fire_now` bypasses the scheduler and dispatches every subscription immediately.

### 7. Watch the run
`workspaces::list_workspace_sessions`
```json
{ "workspace_id": "ws-1" }
```
Response:
```json
{ "items": [ { "id": "ses-1", "status": "running" } ] }
```
Then poll one session:

`workspaces::get_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "ended", "ended_reason": "completed" }
```

## Verify
The fired session ends with `ended_reason: "completed"`, and its transcript shows each PR reviewed plus the GitHub tool calls that posted comments. After a clean Fire now, the hourly cron takes over unattended.

## Gotchas
- GitHub's API rate limit is per-token, not per-call. An agent reviewing 20 PRs in one fire can exhaust a 5000-request budget fast; use a GitHub App token (15000 requests per hour) for production.
- A long review batch keeps the workspace alive past the default TTL. Bump the template TTL or split reviews into smaller batches.
- Workspace state (the cloned repo) persists across sessions on the same instance. Have the agent pull latest at the start of each run to avoid reviewing already-merged commits.
- Changing `config.kind` on an existing trigger is rejected (`type=trigger_kind_immutable`); delete and recreate to switch kinds.

## Related
- `triggers`, `agents`, `workspaces`, `sessions`
- `cookbook/create-and-run-a-session`
- `cookbook/scheduled-summariser`
- `cookbook/daily-incident-digest`
