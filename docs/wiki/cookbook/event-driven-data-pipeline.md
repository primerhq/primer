---
title: "Event-driven data pipeline"
summary: "A scheduled trigger starts an agent that normalises a payload and ingests it into a collection."
order: 8
tools: [system__create_collection, system__create_agent, workspaces__create_workspace, trigger__create, trigger__create_subscription, trigger__fire_now, workspaces__get_workspace_session, system__list_documents]
related: [triggers/subscriptions, knowledge/documents]
---
## Goal

A scheduled trigger fires every five minutes. Each fire starts a fresh agent session that drains an inbox of upstream events, normalises each payload to markdown, and ingests it into a collection. Every clean fire grows a searchable knowledge base.

## Prerequisites

- Run the setup over MCP or from a session in the default workspace (prime). From a session in any other workspace, `workspaces__create_workspace` answers `type=forbidden`, and the calls that name the new workspace answer `type=not-found`.
- An embedding provider id and a semantic search provider id (`vector_store_provider_id`), for the collection.
- A workspace template and provider to materialise a workspace from. The inbox directory lives in the workspace.
- An upstream that writes event files into the workspace's `inbox/` directory: a gateway, a log shipper or a cron pull. A scheduled fire carries no request body, so the fire context holds no upstream payload: the agent drains the inbox.
- A ModelProfile for the agent's model, such as `anthropic-1--claude-sonnet-4-6` ([model profiles](providers/model-profiles)).
- `system__create_agent` and `trigger__create` are approval-gated by default: in a session each call parks until a person approves it; over MCP the call is refused unless an operator retunes the gate ([roles and approvals](start-here/roles-and-approvals)).

## Steps

### 1. Create the ingestion collection

Create the searchable collection with `system__create_collection`.

```json
{"toolset_id": "system", "tool_name": "create_collection", "arguments": {"entity": {"id": "ingestion-buffer", "description": "Normalised inbound event payloads", "search": {"embedder": {"provider_id": "hf-embed-1", "model": "all-MiniLM-L6-v2"}, "vector_store_provider_id": "ssp-1"}}}}
```

Response:

```json
{"id": "ingestion-buffer"}
```

The collection reads `state: indexing` until its documents are indexed.

### 2. Create the ingestion agent

Store the agent with `system__create_agent`.

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "ingestion", "description": "Drains the inbox and ingests normalised events", "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "system_prompt": ["Drain the inbox. For each event file under inbox/, derive a stable document path from the payload's id field (events/<id>.md), normalise the payload to clean markdown, write it into the ingestion-buffer collection with system__put_document, and move the file to inbox/done/. Writing the same path replaces the document: the path is the idempotency key, so a catch-up fire does not duplicate."], "tools": ["system__put_document", "system__list_documents"]}}}
```

Response:

```json
{"id": "ingestion"}
```

The call parks for approval in a session. The workspace file tools (including `workspace__exec`, which moves the drained files) compose onto the agent automatically.

### 3. Materialise the workspace

Build the live workspace from the template with `workspaces__create_workspace`.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"id": "ws-ingest", "template_id": "ingest-template"}}
```

Response:

```json
{"id": "ws-ingest", "template_id": "ingest-template", "phase": "pending"}
```

`phase` is `pending` at the answer: the workspace is usable at once (start sessions in it now). The health probe turns it `running` at its first good probe, about 30 seconds later, and `failed` after three missed probes. Thread `ws-ingest` into the subscription.

### 4. Create the scheduled trigger

Create the trigger with `trigger__create` and a `scheduled` config: a five-field cron read in an explicit time zone.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "inbound-ingest", "name": "Inbound ingest", "config": {"kind": "scheduled", "cron": "*/5 * * * *", "timezone": "UTC", "catchup": "one"}}}
```

Response:

```json
{"id": "tr-1f4a8c3d6e92", "slug": "inbound-ingest"}
```

`*/5 * * * *` fires every five minutes in `UTC`. A time without a zone is read as UTC. A webhook trigger fires on an outside POST instead, and its fire context carries the request body. This recipe stays scheduled on purpose: the agent drains the inbox, and a fire with no new files ends fast and ingests nothing.

### 5. Attach the fresh-session subscription

Say what each fire does with `trigger__create_subscription`: start a fresh session of the agent. The rendered `payload_template` is the session's first message.

```json
{"toolset_id": "trigger", "tool_name": "create_subscription", "arguments": {"trigger_id": "tr-1f4a8c3d6e92", "config": {"kind": "agent_fresh_session", "workspace_id": "ws-ingest", "agent_id": "ingestion"}, "payload_template": "Drain the inbox. The fire ran at {{ fired_at }}. Process every file under inbox/, ingest each one, and move the files you ingest to inbox/done/.", "parallelism": "skip"}}
```

Response:

```json
{"id": "sb-7b2c5d8e1f40", "trigger_id": "tr-1f4a8c3d6e92"}
```

A trigger-fired session is autonomous: it ends after its turn. `parallelism` `skip` drops a tick while the previous run is still at work (`skipped_subscription_busy`); `queue` always starts one.

### 6. Fire it now to test

Run the dispatch while you wait with `trigger__fire_now`, as the console's Fire now does.

```json
{"toolset_id": "trigger", "tool_name": "fire_now", "arguments": {"id": "tr-1f4a8c3d6e92"}}
```

Response:

```json
{"skipped": false, "fire_id": "fire-tr-1f4a8c3d6e92-1761330000000", "results": [{"subscription_id": "sb-7b2c5d8e1f40", "ok": true, "skipped": false, "error_code": null, "error_message": null, "artefact_id": "sess-3e7a5b1c9d04"}]}
```

`artefact_id` names the session the fire started. You may fire a trigger you own, or one whose owner ranks at or below you.

### 7. Watch it end

Read one session's state with `workspaces__get_workspace_session`; it answers `{info, status}`.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-ingest", "session_id": "sess-3e7a5b1c9d04"}}
```

Response:

```json
{"info": {"session_id": "sess-3e7a5b1c9d04", "agent_id": "ingestion", "workspace_id": "ws-ingest", "status": "ended", "ended_reason": "completed", "ended_at": "2026-10-24T10:04:12Z"}, "status": "ended"}
```

Re-call on an interval until `status` is `ended`.

### 8. Confirm the documents landed

List a collection's documents by path with `system__list_documents`.

```json
{"toolset_id": "system", "tool_name": "list_documents", "arguments": {"collection_id": "ingestion-buffer", "prefix": "events/"}}
```

Response:

```json
{"documents": [{"path": "events/evt-001.md", "document_id": "document-8f3a2c9d1b7e", "size": 274}]}
```

The answer holds paths and sizes, no bodies: `system__get_document_content` reads a body. The new documents appear after a clean fire.

## Verify

The fired session ends `ended_reason: completed`, the inbox is drained into `inbox/done/`, and `system__list_documents` shows the new normalised documents under `events/`. After a clean fire, the schedule takes over unattended.

## Gotchas

- A burst of upstream events floods the workers when `parallelism` is `queue` and the trigger fires faster than sessions complete. Use `skip`, or scale the worker pool to the expected rate.
- Derive the document path from a stable id in the payload. Without it, a missed tick and the catch-up fire that follows do the work twice: the repeat write replaces the document, but the normalisation runs again.
- A scheduled fire carries no request: a `payload_template` that names `webhook_body` or `webhook_headers` fails that delivery (`payload_template_failed`).
- A payload that is too large for the workspace needs the template's `resources` sized up: Docker and Kubernetes take `disk_bytes`, local takes no size.
- Deleting the workspace destroys the inbox: the events that were not drained are gone. Keep the workspace alive while the schedule runs.

## Related

- [Subscriptions](triggers/subscriptions)
- [Documents](knowledge/documents)
