---
slug: slack-question-answerer
title: Slack question answerer
section: cookbook
summary: A Slack bot that answers questions from a knowledge collection of company docs.
difficulty: beginner
time_minutes: 15
tags: [channels, agents, sessions, knowledge]
---

## Goal

Mention `@answer-bot` in a Slack channel with a question and get an
answer drawn from your indexed knowledge collection of company docs.
No code outside the console.

## Prerequisites

- A Slack workspace with a Slack app already created (you need both the
  App token starting with `xapp-` and the Bot token starting with
  `xoxb-`).
- A knowledge collection named `company-docs` populated with the
  documents the bot should answer from. See Knowledge / Collections if
  you need to create one.

```callout:info
The bot answers from whatever is in `company-docs` at query time. Stale
docs produce stale answers. Set a re-index cadence or re-ingest after
each documentation push.
```

## Steps

### 1. Add a Slack channel provider

1. Open **Channels** in the left nav and switch to the **Providers** tab.
2. Click **New provider** and select **Slack**.
3. Enter the **App token** (`xapp-...`) and **Bot token** (`xoxb-...`).
4. Click **Create provider**.

```embed:channels
```

```callout:warning
Slack requires two distinct tokens. The App token starts with `xapp-`
and the Bot token starts with `xoxb-`. Mixing them causes auth failures
that are not obvious from the platform error message.
```

### 2. Add the Slack channel

1. Switch to the **Channels** tab and click **New channel**.
2. Select the provider you just created.
3. Enter the Slack channel ID (e.g. `C0123ABC456`) in the **External
   ID** field. You can find the channel ID by right-clicking the channel
   name in Slack and choosing **Copy link** -- it is the last path
   segment.
4. Add a label such as `#ops-help` for display purposes.
5. Click **Create channel**.

### 3. Create a workspace

The agent needs a workspace to run in.

1. Open **Workspaces** and click **New workspace**.
2. Select a template (create one first if none exist).
3. Click **Create**.

### 4. Bind the channel to the workspace

1. In Channels, switch to the **Associations** tab and click **New
   association**.
2. Select the workspace and the `#ops-help` channel.
3. Enable **Forward ask_user** if you want the bot to surface approval
   prompts in Slack.
4. Click **Create**.

### 5. Create the agent

1. Open **Agents** and click **New agent**.
2. In the **Basic** tab, set a description such as `answer-bot`, pick
   your LLM provider and model.
3. In the **Tools** tab, check the tools from the `system` toolset so
   the agent can search the knowledge collection, and any tools from
   `web` if docs reference external links.
4. In the **Advanced** tab, enter a system prompt such as: "Answer
   questions from company-docs. If the answer is not in the collection,
   say so plainly."
5. Click **Create**.

```embed:agents-page
```

### 6. Bind the knowledge collection to the agent

Knowledge collections are bound to agents via the agent's tool
configuration. Confirm the collection search tool appears in the
agent's **Tools** tab under the `system` toolset -- if the collection
named `company-docs` is indexed it will appear as a searchable source
on the agent's next session.

### 7. Test the bot

Post `@answer-bot what is the SLA?` in the `#ops-help` channel. Within
a few seconds the bot should reply with the answer drawn from the
collection.

Open **Sessions** in the console to watch the session that was created
by the incoming message. Click the row to inspect the full transcript.

```embed:session-detail
```

## Gotchas

- The `mention_only` behaviour is the default when a channel is
  associated with exactly one agent. Without a mention the bot stays
  silent.
- Slack rate-limits app messages at roughly 1 per second per channel.
  Long answers stream across multiple messages; this is handled
  automatically by the channel adapter.
- The bot strips the `@answer-bot` handle from the text it sends the
  agent. Do not rely on the agent recognising its own name in the
  prompt.

## Automate this

```ref reference/api-channels
The API reference covers channel providers, channels, and workspace
channel associations with full schema detail.
```
