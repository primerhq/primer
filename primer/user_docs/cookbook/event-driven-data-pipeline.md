---
slug: event-driven-data-pipeline
title: Event-driven data pipeline
section: cookbook
summary: A webhook trigger ingests inbound files through an agent into a knowledge collection.
difficulty: advanced
time_minutes: 45
tags: [triggers, agents, knowledge, workspaces]
prerequisites: [features/triggers, features/collections-and-documents, features/workspace-providers]
features: [trigger, agent, knowledge, workspace]
---

## Goal

External systems POST files to a primer webhook. A trigger fires an agent
that normalises the payload and ingests it into a knowledge collection.
Every upstream event becomes searchable within minutes.

## Prerequisites

- An embedding provider and search provider are configured under
  Providers / Embedding (needed to create the collection).
- A workspace template exists.
- An agent exists with the `workspaces` toolset and the knowledge toolset
  bound (so it can write files and call `put_document`).

## Steps

### 1. Create the ingestion collection

1. Open **Knowledge / Collections** in the left nav.
2. Click **New collection**.
3. Set the ID to `ingestion-buffer`.
4. Choose an embedding provider and model.
5. Choose a search provider (pgvector or pgvectorscale).
6. Click **Create**.

```embed:collection-list
```

```callout:warning
The embedding model and search provider are fixed at create time. If you
need to change either, delete the collection and re-ingest all documents.
```

### 2. Create the ingestion agent

1. Open **Agents** in the left nav and click **New agent**.
2. In the **Basic** tab, give the agent the ID `ingestion-bot` and select
   a provider and model.
3. Switch to the **Tools** tab and enable the `workspaces` and `knowledge`
   toolsets.
4. Switch to the **Advanced** tab and set the system prompt:

   ```
   You receive an upstream event payload in the session input.
   Write the raw payload to inbox/<id>.txt in the workspace.
   Normalise the content into clean markdown.
   Call put_document on the ingestion-buffer collection with the
   normalised markdown and a unique idempotency key derived from
   the source payload identifier.
   ```

5. Click **Create**.

### 3. Create the webhook trigger

1. Open **Triggers** in the left nav and click **Create trigger**.
2. In Step 1, select **Scheduled** or **Delayed** -- for a webhook, choose
   the kind that matches your upstream dispatch pattern.

```callout:info
Primer triggers fire on schedule (cron) or at a specific instant (delayed),
not on raw HTTP webhooks. To receive arbitrary POSTs from an upstream
system, expose the primer API behind a lightweight gateway or use a
polling trigger that fetches from a queue.
```

   For a cron-based pull pattern:

   - In Step 2, enter a cron expression such as `*/5 * * * *` (every five
     minutes) and select your timezone.
   - Set the catchup policy to `one` so a single catch-up fires after
     downtime rather than a flood.
3. In Step 3, enter the name `inbound-ingest` and click **Create**.

```embed:trigger-create
```

### 4. Add a subscription to the trigger

1. On the trigger detail page, open the **Subscriptions** panel and click
   **Add subscription**.
2. Choose `agent_fresh_session`.
3. Select the workspace and then `ingestion-bot` as the agent.
4. Optionally write a Jinja2 payload template that passes `{{ fired_at }}`
   and `{{ trigger_id }}` into the session context.
5. Set parallelism to `skip` to drop a tick if the previous run is still
   in-flight, or `queue` to always dispatch.
6. Click **Add subscription**.

### 5. Verify end to end

1. On the trigger detail page, click **Fire now** to dispatch an immediate
   session outside the schedule.
2. Open **Sessions** in the left nav and click the new session row to
   inspect the transcript.
3. Confirm the agent wrote a file to `inbox/` and called `put_document`.
4. Open the `ingestion-buffer` collection detail and click **List documents**
   to confirm the new document appears.

## Result

Every trigger fire starts a fresh agent session that normalises the payload
and writes a searchable document into `ingestion-buffer`. The collection
grows automatically on each tick.

```callout:danger
A burst of upstream events can flood the worker pool if you set parallelism
to `queue` and the trigger fires faster than sessions complete. Use `skip`
parallelism or scale the worker pool to match the expected rate.
```

- The agent should idempotency-key every `put_document` call. A missed tick
  followed by a catch-up fire can re-ingest the same payload if the agent
  does not check for duplicates.
- Large payloads over roughly 10 MB need the workspace template's disk
  allocation set high enough to hold the inbox file. Process large files as
  a stream if possible.

## Automate it

```ref:reference/api-triggers
POST /triggers, subscription management, and fire_now with full schema.
```
