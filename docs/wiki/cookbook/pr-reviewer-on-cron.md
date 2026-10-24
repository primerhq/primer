---
title: "Pull request reviewer on a schedule"
summary: "An hourly scheduled agent reviews new pull requests in a git workspace and comments back."
order: 7
tools: [system__create_agent, workspaces__create_workspace_template, workspaces__create_workspace, trigger__create, trigger__create_subscription, trigger__fire_now, workspaces__list_workspace_sessions, workspaces__get_workspace_session]
related: [triggers/triggers, workspaces/templates]
---
## Goal

An hourly scheduled trigger starts a fresh agent session in a git workspace. The agent lists the new pull requests through its GitHub MCP tools, reviews each diff, and posts review comments back to GitHub.

## Prerequisites

- Run the setup over MCP or from a session in the default workspace (prime). From a session in any other workspace, `workspaces__create_workspace` answers `type=forbidden`, and the calls that name the new workspace answer `type=not-found`.
- A GitHub MCP server configured and reachable. The agent's `tools` list names the server's pull request tools as scoped ids.
- A workspace provider id. `workspaces__list_workspace_provider_choices` answers the ids and kinds you may use.
- A ModelProfile for the agent's model, such as `anthropic-1--claude-sonnet-4-6` ([model profiles](providers/model-profiles)).
- `system__create_agent` and `trigger__create` are approval-gated by default: in a session each call parks until a person approves it; over MCP the call is refused unless an operator retunes the gate ([roles and approvals](start-here/roles-and-approvals)).

## Steps

### 1. Create the review agent

Store the agent with `system__create_agent`.

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "pr-reviewer", "description": "Reviews new GitHub pull requests and posts comments", "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "system_prompt": ["Review new or updated pull requests. Skip the pull requests already listed in .review-state.json in the workspace. For each remaining one, read its diff, post a review comment through the GitHub tools, and record its id in .review-state.json. Keep the review tone consistent across runs."], "tools": ["github__list_pull_requests", "github__get_pull_request", "github__create_pull_request_review"]}}}
```

Response:

```json
{"id": "pr-reviewer"}
```

The call parks for approval in a session. The `tools` names are scoped ids on the GitHub MCP server: the toolset id is that server's toolset id, and the tool names are the server's.

### 2. Create the git-enabled workspace template

Store a materialisation recipe that clones the target repo with `workspaces__create_workspace_template`.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_template", "arguments": {"entity": {"id": "pr-review", "description": "git plus the target repo", "provider_id": "local-1", "backend": {"kind": "local"}, "init_commands": ["git clone https://github.com/acme/app.git repo"]}}}
```

Response:

```json
{"id": "pr-review"}
```

The template's `init_commands` run at materialisation. The workspace state (the clone and the state file) persists across sessions on the same instance.

### 3. Materialise the workspace

Build the live workspace from the template with `workspaces__create_workspace`.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"id": "ws-pr-review", "template_id": "pr-review"}}
```

Response:

```json
{"id": "ws-pr-review", "template_id": "pr-review", "phase": "pending"}
```

`phase` is `pending` at the answer: the workspace is usable at once (start sessions in it now). The health probe turns it `running` at its first good probe, about 30 seconds later, and `failed` after three missed probes. Thread `ws-pr-review` into the subscription.

### 4. Create the hourly trigger

Create the trigger with `trigger__create` and a `scheduled` config: a five-field cron read in an explicit time zone.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "pr-review-hourly", "name": "PR review hourly", "config": {"kind": "scheduled", "cron": "0 * * * *", "timezone": "UTC", "catchup": "one"}}}
```

Response:

```json
{"id": "tr-5b8d2c7e9f31", "slug": "pr-review-hourly"}
```

`0 * * * *` fires at the top of every hour in `UTC`. A time without a zone is read as UTC, so write the zone you mean. `catchup: one` runs only the most recent missed tick after downtime, so no burst of review runs. The call parks for approval in a session.

### 5. Attach the fresh-session subscription

Say what each fire does with `trigger__create_subscription`: start a fresh session of the agent. The rendered `payload_template` is the session's first message.

```json
{"toolset_id": "trigger", "tool_name": "create_subscription", "arguments": {"trigger_id": "tr-5b8d2c7e9f31", "config": {"kind": "agent_fresh_session", "workspace_id": "ws-pr-review", "agent_id": "pr-reviewer"}, "payload_template": "Review the new pull requests. The fire ran at {{ fired_at }}. Read the reviewed ids from .review-state.json in the workspace, review each new or updated pull request, post the comments, and record the ids you reviewed.", "parallelism": "skip"}}
```

Response:

```json
{"id": "sb-2d6e9f1a4c75", "trigger_id": "tr-5b8d2c7e9f31"}
```

A trigger-fired session is autonomous: it ends after its turn. `parallelism` `skip` starts nothing while a review run is still at work (`skipped_subscription_busy`); use `queue` when no tick may be lost.

### 6. Fire it now to test

Run the dispatch while you wait with `trigger__fire_now`, as the console's Fire now does.

```json
{"toolset_id": "trigger", "tool_name": "fire_now", "arguments": {"id": "tr-5b8d2c7e9f31"}}
```

Response:

```json
{"skipped": false, "fire_id": "fire-tr-5b8d2c7e9f31-1761330000000", "results": [{"subscription_id": "sb-2d6e9f1a4c75", "ok": true, "skipped": false, "error_code": null, "error_message": null, "artefact_id": "sess-6c9f2d8e4a13"}]}
```

`artefact_id` names the session the fire started. You may fire a trigger you own, or one whose owner ranks at or below you.

### 7. Find the fired session

Enumerate the sessions on the workspace with `workspaces__list_workspace_sessions`.

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_sessions", "arguments": {"workspace_id": "ws-pr-review"}}
```

Response:

```json
{"items": [{"session_id": "sess-6c9f2d8e4a13", "agent_id": "pr-reviewer", "workspace_id": "ws-pr-review", "status": "running"}], "offset": 0, "length": 1, "total": 1}
```

The fired session also carries `trigger_id`, `subscription_id`, `fire_id` and `fired_at` in its row's metadata.

### 8. Watch it end

Read one session's state with `workspaces__get_workspace_session`; it answers `{info, status}`.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-pr-review", "session_id": "sess-6c9f2d8e4a13"}}
```

Response:

```json
{"info": {"session_id": "sess-6c9f2d8e4a13", "agent_id": "pr-reviewer", "workspace_id": "ws-pr-review", "status": "ended", "ended_reason": "completed", "ended_at": "2026-10-24T10:04:12Z"}, "status": "ended"}
```

Re-call on an interval until `status` is `ended`.

## Verify

The fired session ends `ended_reason: completed`, its transcript shows the GitHub tool calls, the pull request carries the review comment, and `.review-state.json` holds the reviewed ids. The next hourly tick picks up only the new pull requests.

## Gotchas

- GitHub's API rate limit is per token, not per call: an agent that reviews many pull requests in one fire can exhaust the token's budget fast. Use a token with a high limit for many repos.
- The clone goes stale between runs. Have the agent pull latest at the start of each run so it reviews the current diff.
- A local workspace create can be refused: when an operator has told a deployment with separate API and worker processes to refuse local workspaces, step 3 fails and creates nothing. Use a Docker or Kubernetes template instead.
- An update cannot change `config.kind` (`trigger_kind_immutable`): delete the trigger and create a new one to switch kinds. A bad cron or timezone is refused as `cron_invalid` or `timezone_invalid`, and nothing is stored.
- Deleting the workspace destroys the clone and the state file: the next run reviews every open pull request again. Keep the workspace alive while the schedule runs.

## Related

- [Triggers](triggers/triggers)
- [Templates](workspaces/templates)
