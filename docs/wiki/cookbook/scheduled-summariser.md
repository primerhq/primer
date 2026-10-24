---
title: "Scheduled summariser"
summary: "Run an agent every weekday morning to summarise yesterday's logs and post the result to Slack."
order: 5
tools: [system__create_agent, workspaces__create_workspace_template, workspaces__create_workspace, trigger__create, trigger__create_subscription, trigger__fire_now, workspaces__list_workspace_sessions, workspaces__get_workspace_session]
related: [triggers/triggers, channels/reply-binding]
---
## Goal

Every weekday at 09:00 in the trigger's time zone, a scheduled trigger starts a fresh agent session in a log workspace. The agent reads yesterday's log files and ends with the summary as its final answer, which the workspace's reply binding posts to a bound Slack channel.

## Prerequisites

- Run the setup over MCP or from a session in the default workspace (prime). From a session in any other workspace, `workspaces__create_workspace` answers `type=forbidden`, and the calls that name the new workspace answer `type=not-found`.
- A workspace provider id. `workspaces__list_workspace_provider_choices` answers the ids and kinds you may use.
- A ModelProfile for the agent's model, such as `anthropic-1--claude-sonnet-4-6` ([model profiles](providers/model-profiles)).
- A Slack channel the agent can reach: the workspace's reply binding set to it with `system__set_reply_binding`, so each session's final answer posts to the channel ([reply binding](channels/reply-binding)).
- `system__create_agent` and `trigger__create` are approval-gated by default: in a session each call parks until a person approves it; over MCP the call is refused unless an operator retunes the gate ([roles and approvals](start-here/roles-and-approvals)).

## Steps

### 1. Create the summariser agent

Store the agent with `system__create_agent`. The workspace file tools compose onto the agent automatically when a session attaches to the workspace; the rest comes from its `tools` list.

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "log-summariser", "description": "Summarises yesterday's workspace logs every weekday", "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "system_prompt": ["Read the log files under logs/ that changed in the 24 hours before now. Write a short summary grouped by area of the system, with errors and repeated failures flagged."], "tools": ["misc__get_datetime"]}}}
```

Response:

```json
{"id": "log-summariser"}
```

The call parks for approval in a session. The agent reads the current date with `misc__get_datetime` and knows what "yesterday" covers.

### 2. Create the log workspace template

Store a reusable materialisation recipe with `workspaces__create_workspace_template`.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_template", "arguments": {"entity": {"id": "log-reader", "description": "Log workspace for the weekday summariser", "provider_id": "local-1", "backend": {"kind": "local"}}}}
```

Response:

```json
{"id": "log-reader"}
```

A local backend runs on the server itself; it has no image and no size. A file whose source `kind` is `secret` needs an admin.

### 3. Materialise the workspace

Build the live workspace from the template with `workspaces__create_workspace`.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"id": "ws-logs", "template_id": "log-reader"}}
```

Response:

```json
{"id": "ws-logs", "template_id": "log-reader", "phase": "pending"}
```

`phase` is `pending` at the answer: the workspace is usable at once (start sessions in it now). The health probe turns it `running` at its first good probe, about 30 seconds later, and `failed` after three missed probes. Thread `ws-logs` into the subscription.

### 4. Create the scheduled trigger

Create the trigger with `trigger__create` and a `scheduled` config: a five-field cron read in an explicit time zone.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "weekday-log-digest", "name": "Weekday log digest", "config": {"kind": "scheduled", "cron": "0 9 * * 1-5", "timezone": "Europe/London", "catchup": "one"}}}
```

Response:

```json
{"id": "tr-9d3f0a1b2c4e", "slug": "weekday-log-digest"}
```

`0 9 * * 1-5` is 09:00 on weekdays in `Europe/London`. A time without a zone is read as UTC, so write the zone you mean. The call parks for approval in a session. Saving the trigger makes you its owner, and a fired session runs at no more than the lower of the subscription owner's and the trigger owner's current roles.

### 5. Attach the fresh-session subscription

Say what each fire does with `trigger__create_subscription`: start a fresh session of the agent. The rendered `payload_template` is the session's first message.

```json
{"toolset_id": "trigger", "tool_name": "create_subscription", "arguments": {"trigger_id": "tr-9d3f0a1b2c4e", "config": {"kind": "agent_fresh_session", "workspace_id": "ws-logs", "agent_id": "log-summariser"}, "payload_template": "Summarise yesterday's logs. The fire ran at {{ fired_at }}. Read the files under logs/ in the workspace, cover the 24 hours before this fire, and end with the summary as your final answer.", "parallelism": "skip"}}
```

Response:

```json
{"id": "sb-4c1d9e2f7a80", "trigger_id": "tr-9d3f0a1b2c4e"}
```

A trigger-fired session is autonomous: it ends after its turn. `parallelism` `skip` starts nothing while a summariser run is still at work (`skipped_subscription_busy`); use `queue` when no fire may be lost.

### 6. Fire it now to test

Run the dispatch while you wait with `trigger__fire_now`, as the console's Fire now does.

```json
{"toolset_id": "trigger", "tool_name": "fire_now", "arguments": {"id": "tr-9d3f0a1b2c4e"}}
```

Response:

```json
{"skipped": false, "fire_id": "fire-tr-9d3f0a1b2c4e-1761330000000", "results": [{"subscription_id": "sb-4c1d9e2f7a80", "ok": true, "skipped": false, "error_code": null, "error_message": null, "artefact_id": "sess-8f2a1c6d4e90"}]}
```

`artefact_id` names the session the fire started. You may fire a trigger you own, or one whose owner ranks at or below you.

### 7. Find the fired session

Enumerate the sessions on the workspace with `workspaces__list_workspace_sessions`.

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_sessions", "arguments": {"workspace_id": "ws-logs"}}
```

Response:

```json
{"items": [{"session_id": "sess-8f2a1c6d4e90", "agent_id": "log-summariser", "workspace_id": "ws-logs", "status": "running", "started_at": "2026-10-24T09:00:01Z"}], "offset": 0, "length": 1, "total": 1}
```

The fired session also carries `trigger_id`, `subscription_id`, `fire_id` and `fired_at` in its row's metadata.

### 8. Watch it end

Read one session's state with `workspaces__get_workspace_session`; it answers `{info, status}`.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-logs", "session_id": "sess-8f2a1c6d4e90"}}
```

Response:

```json
{"info": {"session_id": "sess-8f2a1c6d4e90", "agent_id": "log-summariser", "workspace_id": "ws-logs", "status": "ended", "ended_reason": "completed", "ended_at": "2026-10-24T09:04:12Z"}, "status": "ended"}
```

Re-call on an interval until `status` is `ended`.

## Verify

The fired session ends `ended_reason: completed`, its transcript shows the log reads and the summary, and the Slack thread for the session (opened with its first post, `Agent session <id>`) ends with the digest. From here the weekday fires run unattended; `trigger__get` on the trigger reads `next_fire_at` for the next one.

## Gotchas

- The summary reaches Slack only through the reply binding. A workspace with no reply binding posts to no channel, and a session that ends without a final answer posts nothing.
- A local workspace create can be refused: when an operator has told a deployment with separate API and worker processes to refuse local workspaces, step 3 fails and creates nothing. Use a Docker or Kubernetes template instead.
- `catchup` decides what a missed tick does after downtime: `none` skips it, `one` fires the most recent one, `all` fires each (at most 64, oldest first). The tick that is due then fires once, in every policy.
- An update cannot change `config.kind` (`trigger_kind_immutable`): delete the trigger and create a new one to switch kinds. A bad cron or timezone is refused as `cron_invalid` or `timezone_invalid`, and nothing is stored.
- If the workspace's health probes keep failing, the row reads `failed` and a fired session cannot start in it. Check the workspace phase before you rely on the schedule.

## Related

- [Triggers](triggers/triggers)
- [Reply binding](channels/reply-binding)
