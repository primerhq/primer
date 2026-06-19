---
slug: discord-moderation-helper
title: Discord moderation helper
section: cookbook
summary: A Discord bot that flags borderline content for a human moderator and waits for an approval click.
difficulty: intermediate
time_minutes: 25
tags: [channels, approvals, agents]
---

## Goal

Every new Discord message in a moderated channel runs through a
classifier agent. Borderline content fires a tool-approval prompt
to a moderator; clear content passes through silently; obviously
bad content auto-deletes.

## Prerequisites

- A Discord channel provider already configured under **Channels**.
  If you have not done this yet, see the channels feature guide.
- An agent named `moderator` bound to a toolset that includes a
  `delete_message` tool.

```ref:channels/channels
Configure a Discord provider and create a channel before following
this recipe.
```

```ref:toolsets/toolsets-approvals
Approval policies gate specific tool calls behind a manual or
automated decision.
```

## Steps

### 1. Add the Discord channel provider

1. Open **Channels** in the left nav, then the **Providers** tab.
2. Click **New provider**, select **Discord**, and paste the bot
   token from the Discord Developer Portal (no `Bot ` prefix).
3. Click **Create provider**.

### 2. Create the channel

1. Switch to the **Channels** tab and click **New channel**.
2. Select the Discord provider you just created.
3. Enter the channel snowflake of the text channel to moderate.
4. Click **Create channel**.

### 3. Bind the channel to a workspace

1. Open the **Associations** tab and click **New association**.
2. Select the workspace that hosts the `moderator` agent and the
   channel above.
3. Enable **Forward tool_approval** so moderator approval prompts
   arrive in Discord.
4. Click **Create**.

```embed:channels
```

### 4. Create the approval policy

1. Open **Approvals** in the left nav and click **Policies**.
2. Click **New policy** (top right).
3. Set type to **Required**, id to `require-delete-message`, toolset
   to the Discord toolset, and tool name to `delete_message`.
4. Leave timeout blank to use the global yield cap.
5. Click **Create policy**.

The `Required` kind means every `delete_message` call parks the
session and waits for a manual decision.

```callout:info
Use the **LLM judge** approval type once you trust the classifier's
confidence calibration. The judge prompt can auto-allow deletions
when the classifier confidence exceeds a threshold, reducing
moderator load.
```

### 5. Configure the trigger subscription

1. Open **Triggers** in the left nav and click **Create trigger**.
2. Choose **Scheduled** kind if you want periodic sweeps, or skip
   this step if the channel adapter fires the agent directly on
   incoming messages (the association handles that routing).

For immediate message-by-message moderation, the channel
association alone routes each incoming Discord message to the
`moderator` agent without a separate trigger.

## Verification

When a borderline message arrives, the moderator agent parks on the
`delete_message` call and the pending approval appears in
**Approvals > Pending**. Each row shows the tool arguments, the
parked session link, and remaining time.

1. Open **Approvals > Pending**.
2. Click **Approve** to delete the message, or **Reject** with a
   reason to keep it and let the agent receive the rejection.

You can also approve or reject directly from the session transcript:
an amber banner appears at the top when the session is parked on an
approval.

```embed:session-detail
```

## Gotchas

```callout:danger
A pattern that matches every message (including the moderator
bot's own output) creates a loop. Either scope the channel
association to exclude the bot's user ID, or instruct the agent in
its system prompt to skip messages where the author is itself.
```

- Discord gateway delivery is slightly delayed (1 to 5 seconds
  typical). Calibrate the approval timeout against the moderator's
  expected response window.
- The approval queue grows if the moderator is away. Set a timeout
  on the policy so unresponded items auto-reject rather than
  blocking the session indefinitely.
- Deleting an approval policy does not resolve already-parked
  sessions. Decide them manually on the Pending tab first.

## Automate it

```ref:reference/api-tool-approval
Create and manage approval policies and respond to pending calls
programmatically via the REST API.
```
