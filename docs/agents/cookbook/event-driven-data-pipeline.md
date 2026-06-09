---
slug: cookbook/event-driven-data-pipeline
title: Event Driven Data Pipeline
summary: Fire a scheduled trigger whose subscription starts a fresh agent that normalises an inbound payload and ingests it into a knowledge collection.
mcp_tools:
  - system::create_collection
  - system::create_agent
  - workspaces::create_workspace
  - trigger::create
  - trigger::create_subscription
  - trigger::fire_now
  - workspaces::get_workspace_session
  - system::find_documents
---

## Goal
Stand up a tick-driven ingestion pipeline. A trigger fires on a cron schedule; its subscription starts a fresh agent session that writes the inbound payload into the workspace, normalises it to markdown, and calls `put_document` against an `ingestion-buffer` collection. Every fire grows a searchable knowledge base.

## Prerequisites
- An embedding provider and a search provider are configured (needed to create the collection).
- A workspace template and provider exist to materialise a workspace from.
- Permission to create collections, agents, workspaces, and triggers over MCP.

```callout:info
Primer triggers fire on a schedule (cron) or at a specific instant (delayed), not on raw HTTP webhooks. To drive ingestion from an upstream system, either expose the primer API behind a lightweight gateway or, as here, run a cron pull that the agent uses to drain a queue or inbox.
```

## Steps
### 1. Create the ingestion collection
`system::create_collection`
```json
{
  "entity": {
    "id": "ingestion-buffer",
    "description": "Normalised inbound payloads",
    "embedder": { "provider_id": "hf-1", "model": "all-MiniLM-L6-v2" },
    "search_provider_id": "ssp-1"
  }
}
```
Response:
```json
{ "id": "ingestion-buffer" }
```
The embedder and search provider are fixed at create time. To change either, delete the collection and re-ingest.

### 2. Create the ingestion agent
`system::create_agent`
```json
{
  "entity": {
    "id": "ingestion-bot",
    "description": "Normalises an event payload and ingests it",
    "system_prompt": ["You receive an upstream event payload in the session input. Write the raw payload to inbox/<id>.txt in the workspace, normalise it to clean markdown, then call put_document on the ingestion-buffer collection with a unique idempotency key derived from the source payload identifier."],
    "model": { "provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6" }
  }
}
```
Response:
```json
{ "id": "ingestion-bot" }
```
Bind the `workspaces` toolset (to write the inbox file) and the knowledge toolset (to call `put_document`) on the agent so the run can do both. Instruct the agent to idempotency-key every `put_document`; a catch-up fire after a missed tick can otherwise re-ingest the same payload.

### 3. Materialise the workspace
`workspaces::create_workspace`
```json
{ "template_id": "ingest-template" }
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
  "slug": "inbound-ingest",
  "name": "Inbound ingest",
  "config": { "kind": "scheduled", "cron": "*/5 * * * *", "catchup": "one" },
  "enabled": true
}
```
Response:
```json
{ "id": "trg-1", "slug": "inbound-ingest" }
```
`*/5 * * * *` fires every five minutes. `catchup: "one"` fires a single catch-up after downtime rather than a flood. Thread `id` ("trg-1") into the subscription.

### 5. Attach an agent_fresh_session subscription
`trigger::create_subscription`
```json
{
  "trigger_id": "trg-1",
  "config": {
    "kind": "agent_fresh_session",
    "agent_id": "ingestion-bot",
    "workspace_id": "ws-1"
  }
}
```
Response:
```json
{ "id": "sub-1", "trigger_id": "trg-1" }
```
Each tick starts a brand-new session bound to the agent in this workspace. Set `parallelism` to `skip` (via `update_subscription`) to drop a tick when the previous run is still in flight, or `queue` to always dispatch.

### 6. Test with Fire now
`trigger::fire_now`
```json
{ "id": "trg-1" }
```
Response:
```json
{ "fire_id": "fire-1", "results": [ { "subscription_id": "sub-1", "session_id": "ses-1" } ] }
```
`fire_now` bypasses the scheduler and dispatches the subscription immediately.

### 7. Watch the run end
`workspaces::get_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "ended", "ended_reason": "completed" }
```
Re-call on an interval until `status` is `ended`.

### 8. Confirm the document landed
`system::find_documents`
```json
{
  "predicate": {
    "left": { "name": "collection_id" },
    "op": "=",
    "right": { "value": "ingestion-buffer" }
  }
}
```
Response:
```json
{ "items": [ { "id": "doc-1", "collection_id": "ingestion-buffer" } ] }
```
`find_documents` matches a predicate tree; filter on `collection_id` to list this collection's documents. The new document should appear after a clean fire.

## Verify
The fired session ends with `ended_reason: "completed"`, the agent wrote a file under `inbox/`, and `find_documents` on `ingestion-buffer` shows the new normalised document. After a clean Fire now, the cron takes over unattended.

## Gotchas
- A burst of upstream events floods the worker pool if `parallelism` is `queue` and the trigger fires faster than sessions complete. Use `skip` parallelism or scale the worker pool to match the expected rate.
- Without an idempotency key, a missed tick followed by a catch-up fire can re-ingest the same payload. Have the agent derive the key from the source payload identifier.
- Large payloads (roughly 10 MB and up) need the workspace template's disk allocation set high enough to hold the inbox file. Stream large files where possible.
- Changing `config.kind` on an existing trigger is rejected (`type=trigger_kind_immutable`); delete and recreate to switch kinds.

## Related
- `triggers`, `agents`, `knowledge`, `workspaces`, `sessions`
- `cookbook/pr-reviewer-on-cron`
- `cookbook/create-and-run-a-session`
