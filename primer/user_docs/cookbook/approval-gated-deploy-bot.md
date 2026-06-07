---
slug: approval-gated-deploy-bot
title: Approval-gated deploy bot
section: cookbook
summary: A Slack-driven deploy bot whose actual deploy step requires an explicit operator approval click.
difficulty: advanced
time_minutes: 45
tags: [channels, tool-approval, agents, triggers]
prerequisites: [features/channels, features/tool-approval, features/agents]
features: [channel, agent, tool-approval, trigger]
---

## Goal

An operator messages a Slack channel with a deploy command. An agent plans
the deploy, then the actual deploy step parks until a human clicks Approve
in the primer console (or in Slack, if the channel association has
`forward_tool_approval` enabled). The deploy runs only after explicit
approval; a Reject returns a clean error to the agent.

## Prerequisites

- A Slack channel provider is configured under Channels / Providers.
- A Slack channel is created and bound to a workspace (Channels /
  Associations, with **Forward tool approval** enabled on the association).
- A `deploy-tools` toolset is registered with a `deploy_prod` tool.
- An LLM provider is configured.

## Steps

### 1. Create the deploy agent

1. Open **Agents** in the left nav and click **New agent**.
2. In the **Basic** tab, set the ID to `deploy-bot`, choose a provider and
   model.
3. Switch to the **Tools** tab and enable the `deploy-tools` toolset and
   the `channels` toolset (so the agent can post results back to Slack).
4. Switch to the **Advanced** tab and set a system prompt such as:

   ```
   You are a deploy coordinator. When given a deploy target, produce a
   concise plan (services, migrations, cache flushes). Then call
   deploy_prod with the target. After the call resolves, post the
   outcome back to the originating channel.
   ```

5. Click **Create**.

### 2. Create a required approval policy for deploy_prod

1. Open **Approvals** in the left nav and click the **Policies** tab.
2. Click **New policy**.
3. Select **Required** as the approval type (every call parks and waits for
   a manual decision).
4. Set the ID to `gate-deploy-prod`.
5. Select `deploy-tools` as the toolset.
6. Enter `deploy_prod` as the tool name.
7. Optionally set a timeout in seconds. Sessions parked past the timeout
   receive an automatic rejection.
8. Click **Create policy**.

```callout:danger
The deploy_prod tool is irreversible. Test the gate-pause path in a
development session before enabling this policy in production. A rejection
sends a clean error to the agent; verify the agent's prompt handles that
error gracefully so the session ends cleanly rather than stalling.
```

### 3. Create a trigger to watch for the deploy command

Primer triggers are cron or delayed; they do not match channel message
patterns natively. The recommended pattern is a short cron trigger that
polls a command queue, or a gateway script that calls `POST /v1/sessions`
directly when a matching Slack message arrives.

For a polling approach:

1. Open **Triggers** in the left nav and click **Create trigger**.
2. Select **Scheduled** in Step 1.
3. In Step 2, enter a cron expression (`* * * * *` for every minute) and
   a timezone.
4. In Step 3, name it `deploy-command-poll` and click **Create**.
5. Add a subscription of kind `agent_fresh_session`, select the workspace
   and `deploy-bot`, and click **Add subscription**.

For a direct approach, your gateway POSTs to
`/v1/sessions` with `agent_id=deploy-bot` whenever Slack delivers a
matching slash command, skipping the trigger entirely.

### 4. Verify the approval gate

1. Start a session manually: open **Sessions** in the left nav, click
   **New session**, pick the workspace and `deploy-bot`, and type a deploy
   target in the input field.
2. Watch the session transcript. When the agent calls `deploy_prod`, the
   session status changes to **parked**.
3. Open the **Pending** tab on the Approvals page. The `deploy_prod` call
   appears with the tool arguments, session link, and elapsed wait time.
4. Click **Approve**. The session resumes, `deploy_prod` dispatches, and
   the agent posts the outcome.
5. Repeat and click **Reject**, entering a rejection reason. Confirm the
   agent receives the error and ends the session cleanly.

```embed:session-detail
```

```callout:warning
The approval prompt surfaces in the primer console on the Pending tab and,
if the channel association has **Forward tool approval** enabled, in Slack.
In high-traffic channels the Slack prompt can scroll away before the
approver sees it. Pin approval responses to a dedicated moderator channel
by creating a second association with `forward_tool_approval` enabled and
`forward_ask_user` disabled.
```

## Result

Any session that calls `deploy_prod` parks automatically. No deploy runs
without a manual Approve click. The Approvals page shows every pending and
historical decision with its arguments, approver, and timestamp.

- Plan generation plus the approval wait adds latency proportional to
  the approver's response time. Set a timeout on the policy to auto-reject
  calls that wait too long and page the on-call team separately.
- If the approver is unavailable, the parked session holds a worker slot.
  Either set a policy timeout or monitor the Pending tab with an alert.

## Automate it

```ref:reference/api-tool-approval
Full schema for approval policies and the respond endpoint, including Rego
policy shape and LLM judge fields.
```
