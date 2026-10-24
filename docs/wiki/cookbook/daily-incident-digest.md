---
title: "Daily incident digest"
summary: "Every weekday, an agent reads overnight incidents from a collection and posts a digest to a Slack channel."
order: 6
tools: [system__create_collection, system__create_agent, system__create_channel, trigger__create, trigger__create_subscription, trigger__fire_now, workspaces__list_workspace_sessions, workspaces__get_workspace_session]
related: [triggers/subscriptions, knowledge/collections]
---
## Goal

Every weekday at 09:00 in the trigger's time zone, a scheduled trigger starts a fresh agent session. The agent reads the overnight incident write-ups from an indexed collection, groups them by severity, and ends with the digest as its final answer, which the workspace's reply binding posts to the ops Slack channel.

## Prerequisites

- Run the setup over MCP or from a session in the default workspace (prime). From a session in another workspace, the calls that name a workspace or a session outside it answer `type=not-found`.
- A materialised workspace (see [create and run a session](cookbook/create-and-run-a-session)). Its id is threaded into the subscription.
- A channel provider already configured, for the ops Slack channel.
- An embedding provider id and a semantic search provider id (`vector_store_provider_id`), for the collection.
- A ModelProfile for the agent's model, such as `anthropic-1--claude-sonnet-4-6` ([model profiles](providers/model-profiles)).
- `system__create_agent` and `trigger__create` are approval-gated by default: in a session each call parks until a person approves it; over MCP the call is refused unless an operator retunes the gate ([roles and approvals](start-here/roles-and-approvals)).

## Steps

### 1. Create the incident collection

Create the searchable collection with `system__create_collection`.

```json
{"toolset_id": "system", "tool_name": "create_collection", "arguments": {"entity": {"id": "incidents", "description": "Overnight incident write-ups", "search": {"embedder": {"provider_id": "hf-embed-1", "model": "all-MiniLM-L6-v2"}, "vector_store_provider_id": "ssp-1"}}}}
```

Response:

```json
{"id": "incidents"}
```

The collection reads `state: indexing` until its documents are indexed. Put each write-up in with the knowledge tools (`system__put_document` or `collections__create_document`), and carry at least `severity` and `started_at` in its meta so the agent can filter by recency.

### 2. Create the digest agent

Store the agent with `system__create_agent`.

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "incident-digest", "description": "Posts a severity-grouped overnight incident digest", "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "system_prompt": ["You post an overnight incident digest. Search the incidents collection for the write-ups the message names, group them by severity, and end with the digest: a count per severity, the notable incidents, and each one's document path."], "tools": ["system__search_collection", "system__list_collection_documents"]}}}
```

Response:

```json
{"id": "incident-digest"}
```

The call parks for approval in a session. The `tools` list names the knowledge tools the run uses; everything else the agent needs in a workspace comes from the workspace file tools.

### 3. Create the Slack channel row

Register the channel row with `system__create_channel`. The row names the provider and the channel's external id.

```json
{"toolset_id": "system", "tool_name": "create_channel", "arguments": {"entity": {"id": "ops-incidents", "provider_id": "cp-slack-1", "provider": "slack", "external_id": "C09AB12CD"}}}
```

Response:

```json
{"id": "ops-incidents"}
```

Point the workspace's reply binding at the new row with `system__set_reply_binding`. From then on, each session's final answer posts to the channel ([reply binding](channels/reply-binding)).

### 4. Create the scheduled trigger

Create the trigger with `trigger__create` and a `scheduled` config: a five-field cron read in an explicit time zone.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "incident-digest", "name": "Incident digest weekday", "config": {"kind": "scheduled", "cron": "0 9 * * 1-5", "timezone": "Asia/Dubai", "catchup": "one"}}}
```

Response:

```json
{"id": "tr-7c2e9d4f1a60", "slug": "incident-digest"}
```

`0 9 * * 1-5` is 09:00 on weekdays in `Asia/Dubai`. A time without a zone is read as UTC, so write the zone you mean. The call parks for approval in a session.

### 5. Attach the fresh-session subscription

Say what each fire does with `trigger__create_subscription`: start a fresh session of the agent. The rendered `payload_template` is the session's first message.

```json
{"toolset_id": "trigger", "tool_name": "create_subscription", "arguments": {"trigger_id": "tr-7c2e9d4f1a60", "config": {"kind": "agent_fresh_session", "workspace_id": "ws-ops", "agent_id": "incident-digest"}, "payload_template": "Post the overnight incident digest. The fire ran at {{ fired_at }}. Search the incidents collection for write-ups that started since 22:00 the day before this fire, group them by severity, and end with the digest.", "parallelism": "skip"}}
```

Response:

```json
{"id": "sb-9a3f1c8e5d20", "trigger_id": "tr-7c2e9d4f1a60"}
```

A trigger-fired session is autonomous: it ends after its turn. `parallelism` `skip` starts nothing while a digest run is still at work (`skipped_subscription_busy`).

### 6. Fire it now to test

Run the dispatch while you wait with `trigger__fire_now`, as the console's Fire now does.

```json
{"toolset_id": "trigger", "tool_name": "fire_now", "arguments": {"id": "tr-7c2e9d4f1a60"}}
```

Response:

```json
{"skipped": false, "fire_id": "fire-tr-7c2e9d4f1a60-1761330000000", "results": [{"subscription_id": "sb-9a3f1c8e5d20", "ok": true, "skipped": false, "error_code": null, "error_message": null, "artefact_id": "sess-1b4d7e9f2a60"}]}
```

`artefact_id` names the session the fire started. You may fire a trigger you own, or one whose owner ranks at or below you.

### 7. Find the fired session

Enumerate the sessions on the workspace with `workspaces__list_workspace_sessions`.

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_sessions", "arguments": {"workspace_id": "ws-ops"}}
```

Response:

```json
{"items": [{"session_id": "sess-1b4d7e9f2a60", "agent_id": "incident-digest", "workspace_id": "ws-ops", "status": "running"}], "offset": 0, "length": 1, "total": 1}
```

The fired session also carries `trigger_id`, `subscription_id`, `fire_id` and `fired_at` in its row's metadata.

### 8. Watch it end

Read one session's state with `workspaces__get_workspace_session`; it answers `{info, status}`.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-ops", "session_id": "sess-1b4d7e9f2a60"}}
```

Response:

```json
{"info": {"session_id": "sess-1b4d7e9f2a60", "agent_id": "incident-digest", "workspace_id": "ws-ops", "status": "ended", "ended_reason": "completed", "ended_at": "2026-10-24T09:04:12Z"}, "status": "ended"}
```

Re-call on an interval until `status` is `ended`.

## Verify

The fired session ends `ended_reason: completed`, its transcript shows the collection search and the digest, and the Slack thread for the session (opened with its first post, `Agent session <id>`) ends with the severity-grouped digest. The collection holds the write-ups, and from here the weekday fires run unattended.

## Gotchas

- Put write-ups in the collection before the first fire. An empty collection makes every digest report nothing overnight.
- The recency filter comes from the documents' meta (`started_at`), not from the query text. Stamp the timestamp accurately at ingest time.
- The digest is the session's final answer. The reply binding posts the final answer and each question the session asks; a session that ends without an answer posts nothing.
- An update cannot change `config.kind` (`trigger_kind_immutable`): for weekend coverage, create a second trigger with `0 9 * * 0,6` and the same subscription, or change the cron to `0 9 * * *` from the start.
- A fired session runs as the subscription's owner, at no more than the lower of the subscription owner's and the trigger owner's current roles. A gone owner (a deleted or disabled account) fails the fire with `owner_unresolvable` and starts nothing.

## Related

- [Subscriptions](triggers/subscriptions)
- [Collections](knowledge/collections)
