---
slug: event-driven-data-pipeline
title: Event-driven data pipeline
section: cookbook
summary: A webhook trigger ingests inbound files through an agent into a knowledge collection.
difficulty: advanced
time_minutes: 45
tags: [triggers, agents, knowledge, workspaces]
prerequisites: [features/triggers, embedding/collections-and-documents, workspaces/workspace-providers]
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
   Call put_document on the ingestion-buffer collection, using a
   stable path derived from the source payload identifier (for
   example events/<id>.md) and the normalised markdown as content.
   Writing the same path twice replaces the document, so the path
   doubles as the idempotency key.
   ```

5. Click **Create**.

### 3. Create the webhook trigger

1. Open **Triggers** in the left nav and click **Create trigger**.
2. In Step 1, select **Webhook**.
3. In Step 2, optionally set an **HMAC secret**. When set, every inbound
   request must carry an `X-Primer-Signature: sha256=<hex>` header computed
   over the raw body with that secret; unsigned or mismatched requests are
   rejected 401. Leave it blank to authenticate by the URL token alone.
4. In Step 3, enter the name `inbound-ingest` and click **Create**.

```embed:trigger-create
```

On the trigger detail page, copy the **webhook URL**. It looks like
`https://<your-primer-origin>/v1/webhooks/<token>`, where `<token>` is a
server-minted 32-character secret shown only here. Point your upstream
system at this URL with an HTTP `POST` and a JSON body.

```callout:warning
The URL token is the credential, so treat the webhook URL as a secret and
rotate it from the detail page if it leaks. For untrusted senders set an
HMAC secret and verify the `X-Primer-Signature` header.
```

### 4. Add a subscription to the trigger

1. On the trigger detail page, open the **Subscriptions** panel and click
   **Add subscription**.
2. Choose `agent_fresh_session`.
3. Select the workspace and then `ingestion-bot` as the agent.
4. Write a Jinja2 payload template that passes the inbound payload into the
   session context, for example `{{ webhook_body }}` (also available:
   `webhook_headers`, `webhook_query`, `webhook_method`). The agent reads
   this as its session input.
5. Set parallelism to `skip` to drop an event if the previous run is still
   in-flight, or `queue` to always dispatch.
6. Click **Add subscription**.

### 5. Verify end to end

1. POST a sample payload to the webhook URL:

```code-tabs:bash
--- bash
curl -X POST https://<your-primer-origin>/v1/webhooks/<token> \
  -H "Content-Type: application/json" \
  -d '{"id": "evt-001", "text": "hello from upstream"}'
```

   The endpoint returns `202 Accepted` immediately and dispatches the
   session in the background. (You can also click **Fire now** on the
   trigger detail page to test with an empty payload.)
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

- The agent should derive each document's `path` from a stable field in
  `webhook_body`: an upstream system that retries on a slow response can
  deliver the same event twice, and writing to the same path replaces the
  document rather than creating a duplicate, so the path is the idempotency
  key.
- The webhook body is capped at 1 MB and rate-limited to 60 requests per
  minute per token (a sliding window, approximate across workers). For
  larger or burstier feeds, have the sender push to a queue and POST a
  reference the agent fetches, rather than the whole payload.

## Automate it

```ref:reference/api-triggers
POST /triggers, subscription management, and fire_now with full schema.
```
