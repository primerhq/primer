---
slug: pr-reviewer-on-cron
title: PR reviewer on cron
section: cookbook
summary: Every hour, list new GitHub PRs, run an agent over each, post review comments.
difficulty: intermediate
time_minutes: 30
tags: [triggers, agents, mcp, workspaces]
---

## Goal

A scheduled trigger fires every hour. It starts an agent session in a
workspace that has `git` and the repository available. The agent lists
new pull requests via the GitHub MCP connector, reviews each one, and
posts review comments back to GitHub.

## Prerequisites

- A GitHub MCP server configured and reachable from your primer
  instance.
- A workspace template that has `git` installed and the target repo
  cloned (or the agent's system prompt instructs it to clone on first
  run).
- An agent already created with a review-style system prompt and the
  MCP toolset enabled.

```callout:info
Pin the agent's review tone in the system prompt. Drift between hourly
runs reads as inconsistent to PR authors.
```

## Steps

### 1. Prepare the workspace template

1. Open **Workspaces** in the left nav.
2. If no suitable template exists, click **New workspace** then the
   **Create a template now** inline link.

```embed:workspace-template-form
```

3. Fill in:
   - **Name**: `pr-review-template`
   - **Provider**: your configured workspace provider
   - **Init command**: any first-run setup needed (e.g. cloning the
     repo, installing tools)
4. Click **Create template**, then **Create workspace** to spin up an
   instance.

### 2. Create the agent

1. Open **Agents** and click **New agent**.
2. In the **Basic** tab, enter a description such as `pr-reviewer`, and
   select your LLM provider and model.
3. In the **Tools** tab, enable the MCP toolset that exposes your
   GitHub MCP connector.
4. In the **Advanced** tab, enter a system prompt that instructs the
   agent to list open PRs, review each file diff, and post review
   comments via the available MCP tools.
5. Click **Create**.

```embed:agents-page
```

### 3. Create a scheduled trigger

1. Open **Triggers** and click **Create trigger**.
2. **Kind**: choose **Scheduled**.
3. **Config**: enter cron expression `0 * * * *` (top of every UTC
   hour). Select an appropriate IANA timezone. Set **Catchup policy**
   to `none` so missed ticks during downtime do not cause a burst of
   review runs.
4. **Details**: name it `pr-review-hourly`. Click **Create**.

```embed:trigger-create
```

### 4. Add a subscription

On the trigger detail page, click **Add subscription**.

1. Choose kind **agent_fresh_session**.
2. Select the `pr-review-template` workspace and the `pr-reviewer`
   agent.
3. Set **Parallelism** to `skip`: if a review run is still in flight
   when the next tick arrives, skip the tick rather than stacking
   another run.
4. Click **Add subscription**.

### 5. Verify with Fire now

Click **Fire now** on the trigger detail page. Navigate to **Sessions**
and find the new session row. Click it to open the detail view and
watch the agent's review transcript stream in.

```embed:session-detail
```

A completed run shows each PR reviewed and the GitHub tool calls that
posted comments. Check the **Sessions** tab on the workspace to confirm
the run landed in the correct sandbox.

```callout:success
After a successful Fire now review, the hourly cron takes over.
Subsequent runs are unattended unless the agent encounters an error or
a tool call requires approval.
```

## Gotchas

```callout:warning
GitHub's API rate limit is per-token, not per-call. An agent reviewing
20 PRs in one fire can exhaust a 5000-request budget quickly. Use a
GitHub App token (15000 requests per hour) for production runs.
```

- The cron expression `0 * * * *` fires at the top of every UTC hour.
  Convert to local time for any documentation or runbooks you write.
- Workspaces persist until you delete them (there is no TTL). A long
  review batch simply holds the workspace longer; reuse or delete
  workspaces so they do not accumulate.
- The workspace state (cloned repo) persists across agent sessions on
  the same instance. Pull the latest changes at the start of each run
  to avoid reviewing already-merged commits.

## Automate this

```ref:reference/api-triggers
The API reference covers POST /triggers, subscriptions, and fire_now
with full schema detail.
```

```ref:reference/api-sessions
Session list, control endpoints, and transcript inspection via API.
```
