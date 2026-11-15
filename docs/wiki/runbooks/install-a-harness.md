---
title: Install a harness
summary: Register and fetch a Git bundle, approve exact saved overrides, verify managed rows, then sync or deliberately uninstall.
audience: admin
tools: [harness__register, harness__fetch, harness__update, harness__update_overrides, harness__install, harness__sync, harness__uninstall, harness__get, harness__list, system__describe_tool, system__get_agent, system__get_graph]
related: [harnesses/what-a-harness-is, runbooks/create-an-agent, runbooks/create-and-run-a-graph, runbooks/approval-policy]
---
## Goal

Install a trusted Git bundle, prove its actual managed agents and graphs, and sync or remove only that bundle.

## Preconditions

Runner: admin. Confirm the repository owner, HTTPS URL, pinned ref or reviewed branch, optional subpath, required model profiles and bundle overrides schema with the operator. Stop if credentials or prerequisites are missing; use [agent setup](runbooks/create-an-agent) for the profile check. Never paste a Git token into a prompt or logged call. HTTPS is the default supported transport; file URLs require an explicit operator opt-in and are off by default. Do not enable file URLs to work around an unavailable remote.

Read visible bundles before choosing a unique slug with [`harness__list`](reference/tools/harness/list)
```json
{"toolset_id": "harness", "tool_name": "list", "arguments": {"slug": "team-assistant"}}
```

Read live policies with [`system__describe_tool`](reference/tools/system/describe_tool). Repeat for sync and uninstall before those writes; policy can be retuned.

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "harness", "tool_name": "install"}}
```

## Roles and gates

| operation | shipped gate | authority and approval card |
|---|---|---|
| register, fetch, update, update_overrides | none | admin; registration and preparation do not install managed rows |
| [`harness__install`](reference/tools/harness/install), [`harness__sync`](reference/tools/harness/sync) | required | admin runner and admin-only approvers; exact id and saved overrides |
| [`harness__uninstall`](reference/tools/harness/uninstall) | required | admin runner and admin-only approvers; exact id being removed |
| get, list | none | user tier plus entity access; a user cannot read an inaccessible bundle merely by knowing its id |

A gated worker turn parks until a person eligible under the live policy decides the unique occurrence. Do not retry it or use a REST twin to bypass a parked call. Rejection has no requested effect; approval retains the original caller. Native MCP can return tool_unavailable for a required gate or role/exposure refusal. Read sharing permits inspection, not administrative mutations or approval of an admin-only gate.

## Steps

1. Register the confirmed source with [`harness__register`](reference/tools/harness/register). The returned opaque id identifies a draft, not an installation. These are worked values; confirm the actual reachable repository before executing.

```json
{"toolset_id": "harness", "tool_name": "register", "arguments": {"name": "Team assistant", "slug": "team-assistant", "git_url": "https://github.com/primerhq/team-assistant.git", "ref": "main"}}
```

2. Fetch the source with [`harness__fetch`](reference/tools/harness/fetch), then poll [`harness__get`](reference/tools/harness/get) until pending_operation is null, last_operation_error is clear, and the cached bundle/schema is present. An enqueued fetch is not completion. Inspect the cached schema and required profiles before saving overrides.

```json
{"toolset_id": "harness", "tool_name": "fetch", "arguments": {"id": "hns-42"}}
```

```json
{"toolset_id": "harness", "tool_name": "get", "arguments": {"id": "hns-42"}}
```

3. Save values matching that schema with [`harness__update_overrides`](reference/tools/harness/update_overrides). The example assumes the inspected bundle declares profile_id and description; do not assume every bundle does.

```json
{"toolset_id": "harness", "tool_name": "update_overrides", "arguments": {"id": "hns-42", "overrides": {"profile_id": "team-profile", "description": "Team assistant v1"}}}
```

Reread the row. Install with exactly the stored overrides, so the approval card describes what the worker will render.

```json
{"toolset_id": "harness", "tool_name": "install", "arguments": {"id": "hns-42", "overrides": {"profile_id": "team-profile", "description": "Team assistant v1"}}}
```

4. Wait for the asynchronous operation: pending_operation must clear, status must be installed, last_operation_error must be null, and resolved_commit must identify the fetched source. A returned queued row is not success.

5. To change the source, use [`harness__update`](reference/tools/harness/update) and fetch again, then inspect the new schema. To change values, save new overrides, reread them, then approve sync with those exact values. Sync reconciles the installed bundle; it is not the first-install path.

```json
{"toolset_id": "harness", "tool_name": "update", "arguments": {"id": "hns-42", "ref": "main"}}
```

```json
{"toolset_id": "harness", "tool_name": "update_overrides", "arguments": {"id": "hns-42", "overrides": {"profile_id": "team-profile", "description": "Team assistant v2"}}}
```

```json
{"toolset_id": "harness", "tool_name": "sync", "arguments": {"id": "hns-42", "overrides": {"profile_id": "team-profile", "description": "Team assistant v2"}}}
```

## Verification

Reread [`harness__get`](reference/tools/harness/get) after install and sync. Read each expected managed entity with [`system__get_agent`](reference/tools/system/get_agent) and [`system__get_graph`](reference/tools/system/get_graph). Resolved ids use slug__template_name, not the registration id. Confirm harness_id, rendered values, and rewritten graph agent references. Verify actual changed values after sync, not just a status string. To prove runtime behavior too, follow [agent verification](runbooks/create-an-agent) or [graph execution](runbooks/create-and-run-a-graph).

```json
{"toolset_id": "system", "tool_name": "get_agent", "arguments": {"id": "team-assistant__assistant"}}
```

```json
{"toolset_id": "system", "tool_name": "get_graph", "arguments": {"id": "team-assistant__workflow"}}
```

For deliberate cleanup of this inbound bundle, approve uninstall, then wait until the harness row and its managed agents/graphs are missing. It removes the installed objects; it is not a pause. Do not conflate this tool with the human delete/keep-entities choice. Preserve unrelated entities and the default workspace.

```json
{"toolset_id": "harness", "tool_name": "uninstall", "arguments": {"id": "hns-42"}}
```

## Refusal and failure

- Role/exposure/access refusal: stop and report. A non-owner cannot read a private row; read grants do not enable writes. Do not widen access or replace the initiator to get a green check.
- Required approval or rejection: a person decides the exact parked occurrence; do not retry while parked. Rejection leaves requested state unchanged.
- fetch-required or overrides-schema-missing: finish a real fetch and inspect its cached schema. overrides-invalid or overrides-mismatch: correct the saved values, reread them and seek a new approval; do not substitute arguments after approval.
- conflict/pending operation: wait for completion and reread, rather than enqueueing duplicates. Input digests bind the requested operation to its source and saved values.
- Failed Git authentication, missing bundle files, render/schema errors or last_operation_error: report the exact redacted error and stop. Never treat error status or a partial installed set as completion. Inbound managed rows reject direct CRUD changes; use sync/uninstall. Uninstall failure can retain the row for a deliberate retry after repair.

## Related

[Harness concepts](harnesses/what-a-harness-is), [agent setup and verification](runbooks/create-an-agent), [graph execution](runbooks/create-and-run-a-graph), [approval policy](runbooks/approval-policy).
