---
slug: scheduled-summariser
title: Scheduled summariser
section: cookbook
summary: Wire a trigger that runs an agent every weekday morning and posts the result to Slack.
difficulty: intermediate
time_minutes: 20
tags: [triggers, agents, sessions, scheduled]
---

## Goal

Every weekday at 9 AM, an agent reads yesterday's log files from a
sandbox workspace, summarises them, and posts the summary to a Slack
channel. An `ask_user` prompt then waits for an operator approval click
before the session closes.

The wiring touches four primer subsystems: trigger, agent, session, and
workspace. Each step below maps to one of them.

## Prerequisites

- An agent already configured under Agents with a system prompt that
  knows how to read and summarise logs. See the Agents feature doc if
  you need to create one first.
- A workspace template with a TTL of at least 60 minutes and access to
  the log directory.
- A Slack channel provider already set up under Channels, with a channel
  bound to the workspace via an association that has **Forward ask_user**
  enabled.

```callout:info
Tune the agent's system prompt against real log volume before
scheduling. A noisy first production run is hard to diagnose
after the fact.
```

## Steps

### 1. Create a scheduled trigger

Open **Triggers** in the left nav and click **Create trigger**.

1. **Kind** -- choose **Scheduled**.
2. **Config** -- enter the cron expression `0 5 * * 1-5` (5:00 AM UTC,
   which is 9:00 AM Asia/Dubai on weekdays). Select your IANA timezone
   from the dropdown. Set **Catchup policy** to `one` so a single
   missed tick after downtime fires once on recovery.
3. **Details** -- name the trigger `weekday-summary`. Click **Create**.

```embed:trigger-create
```

The console navigates to the trigger detail page.

### 2. Add a subscription

On the trigger detail page, open the **Subscriptions** panel and click
**Add subscription**.

1. Choose kind **agent_fresh_session**.
2. Select the workspace from the picker, then select your summariser
   agent.
3. Set **Parallelism** to `skip` so a slow run does not stack on itself.
4. Click **Add subscription**.

### 3. Fire now to verify

Click **Fire now** on the trigger detail page. The status panel shows
the fire ID and confirms one subscription was dispatched. Navigate to
**Sessions** to find the newly created session row.

```embed:sessions-list
```

Click the session row to open the detail view and watch the transcript
stream in as the agent reads logs and builds the summary.

### 4. Review the ask_user prompt in Slack

When the agent finishes the summary it issues an `ask_user` prompt.
Because the workspace association has **Forward ask_user** enabled, the
prompt is delivered to the bound Slack channel. The message looks like:

> *weekday-summary* -- Approve yesterday's summary?
> [Approve] [Reject]

Click **Approve** and the session moves to `completed`. Clicking
**Reject** sends the rejection reason back to the agent so it can
revise.

```callout:success
After a successful approval click the trigger is fully wired.
Subsequent weekday fires run unattended; you only see them again if
approval times out or the agent returns an error.
```

## Gotchas

- Workspace TTL must outlast the agent's longest turn. Default 30
  minutes is usually fine; bump to 60 if log volume is large.
- The Slack channel provider needs the `chat:write` and `chat:read`
  scopes. The OAuth flow surfaces this during provider setup.
- Cron expressions are evaluated in the timezone you select in the
  wizard. Double-check the IANA timezone dropdown -- it pre-seeds from
  your browser locale.

## Automate this

```ref reference/api-triggers
The API reference covers POST /triggers, subscriptions, and fire_now
with full schema detail.
```
