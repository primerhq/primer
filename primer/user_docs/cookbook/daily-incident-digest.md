---
slug: daily-incident-digest
title: Daily incident digest
section: cookbook
summary: Every weekday morning, summarise overnight incidents from indexed runbooks and post to a Slack channel.
difficulty: intermediate
time_minutes: 25
tags: [triggers, channels, agents, semantic-search]
---

## Goal

Every weekday at 09:00 local time, a scheduled trigger fires an
agent that pulls overnight incident write-ups from an indexed
knowledge collection and posts a tagged digest to the ops Slack
channel.

## Prerequisites

- A Slack channel provider configured under **Channels** and a
  channel bound to the target workspace.
- A knowledge collection named `incidents` with `severity` and
  `started_at` metadata fields populated on each document.
- An agent named `incident-digest-bot` with access to the
  `incidents` collection.

```ref:features/triggers
Scheduled and delayed triggers, subscription kinds, and the
timezone-aware cron wizard.
```

```ref:channels/channels
Configure a Slack provider and bind it to the workspace so the
agent can post messages.
```

```ref:embedding/collections-and-documents
Create and populate the incidents collection before the first
trigger fires.
```

## Steps

### 1. Create the knowledge collection

1. Open **Knowledge > Collections** and click **New collection**.
2. Set the ID to `incidents`, pick an embedding provider and model,
   and select your vector search provider.
3. Click **Create**.

```embed:collection-list
```

Populate the collection with incident write-ups before continuing.
Each document should carry at least `severity` (e.g. `SEV-1`) and
`started_at` (ISO timestamp) in its metadata so the agent can
filter by recency.

```callout:warning
The collection must be populated before the trigger fires. If it
is empty on first fire, the agent posts "nothing overnight" every
morning until documents are added.
```

### 2. Create the digest agent

1. Open **Agents** and click **New agent**.
2. Set the ID to `incident-digest-bot`, pick an LLM provider and
   model.
3. On the **Tools** tab, select tools from the system toolset that
   cover knowledge search.
4. On the **Advanced** tab, enter a system prompt such as:
   "Search the incidents collection for items where started_at is
   after 22:00 yesterday. Group results by severity and post a
   plain-text digest to the Slack channel."
5. Click **Create**.

```embed:agents-page
```

### 3. Create the scheduled trigger

1. Open **Triggers** and click **Create trigger**.
2. **Step 1:** choose **Scheduled**.
3. **Step 2:** enter cron expression `0 5 * * 1-5`, and select your
   local timezone from the IANA dropdown (09:00 Asia/Dubai =
   05:00 UTC; adjust for your zone). Set catchup policy to `one`.
4. **Step 3:** name the trigger `incident-digest-weekday`.
5. Click **Create**.

```embed:trigger-create
```

```callout:warning
Cron expressions are evaluated in the timezone you select in the
wizard. Verify the next-fire time shown on the trigger detail page
before leaving the console.
```

### 4. Add a subscription

1. On the trigger detail page, open the **Subscriptions** panel and
   click **Add subscription**.
2. Choose **agent_fresh_session**.
3. Select the workspace and the `incident-digest-bot` agent.
4. Optionally set a payload template that injects `{{ fired_at }}`
   so the agent knows the exact fire time.
5. Set parallelism to **skip** so a slow run does not stack.
6. Click **Add subscription**.

## Verification

After the trigger fires, a new session appears in **Sessions** for
`incident-digest-bot`. Open the session to watch the transcript:
the agent searches the collection, groups by severity, and posts
the digest to Slack.

```embed:session-detail
```

Check the Slack channel: the first post should list something like
"Overnight: 2 SEV-1, 4 SEV-2, 11 noise" with links to the full
write-ups.

## Gotchas

- Change `1-5` to `*` in the cron expression for weekend coverage.
- If you want weekend coverage without recreating the trigger
  (trigger schedule is immutable), create a second trigger with
  `0 5 * * 0,6` and the same subscription.
- The semantic query the agent sends is natural-language. Recency
  filtering uses the `started_at` collection metadata, not the
  query text. Ensure incident documents land with accurate
  timestamps at ingest time.

## Automate it

```ref:reference/api-triggers
Create triggers and subscriptions programmatically via the REST API.
```
