---
title: Manage a workspace
summary: Create a workspace from a template, verify its files and a session, then deliberately destroy it with the consequences understood.
audience: admin
tools: [workspaces__list_workspace_templates, workspaces__get_workspace_template, workspaces__create_workspace, workspaces__get_workspace, workspaces__list_workspaces, workspaces__list_workspace_files, workspaces__read_workspace_file, workspaces__write_workspace_file, workspaces__get_workspace_log, workspaces__create_workspace_session, workspaces__get_workspace_session, workspaces__delete_workspace, system__get_agent, system__list_model_profile_choices, system__describe_tool]
related: [purpose-map, workspaces/create-and-delete, workspaces/templates, workspaces/files-and-log, workspaces/prime, sessions/start-a-session, runbooks/workspace-status, runbooks/conventions]
---

## Goal
Create a separate workspace from a readable template, use its files and start an agent session, then verify the result. Deletion is an optional, deliberate final step: it destroys the backend resources, files, session histories and workspace row; it cannot be undone. Obtain the user's agreement for the specific workspace and this consequence before deletion. Keep the caller's default workspace untouched.

## Preconditions
Every check below is a read; finish these checks before any write. The worked values are `local-default` (template), `operator` (agent), and `w7-workspace` (new workspace). Use the actual readable template and agent IDs returned on this install, choose a fresh workspace ID, and carry the workspace/session IDs returned by creation into every subsequent call. An example ID is not evidence that the row exists.

The caller must be able to reach the documented tools. An external MCP client uses the endpoint's `call_tool` meta-tool with the scoped ID and arguments; the envelopes below are the in-platform `system__call_tool` form. If external discovery is empty or calls answer `code=tool_unavailable`, stop and ask the operator to check endpoint exposure, credentials and grants. That response does not identify the cause; do not bypass it through another interface.

1. List templates with `workspaces__list_workspace_templates` ([reference](reference/tools/workspaces/list_workspace_templates)):

```json
{"toolset_id":"workspaces","tool_name":"list_workspace_templates","arguments":{"limit":20}}
```

Expected: readable template rows. Follow the returned pagination until the intended template is found. Read that row using `workspaces__get_workspace_template` ([reference](reference/tools/workspaces/get_workspace_template)):

```json
{"toolset_id":"workspaces","tool_name":"get_workspace_template","arguments":{"id":"local-default"}}
```

Expected: the template's provider and recipe. Do not invent a provider ID or copy masked secrets into overrides. If no usable template exists, stop and ask an admin to configure the provider/template using [workspaces/providers](workspaces/providers) and [workspaces/templates](workspaces/templates). Template retirement stops future creates from that recipe; it is distinct from destroying a materialised workspace.

2. Read the agent with `system__get_agent` ([reference](reference/tools/system/get_agent)) and its available profile choices with `system__list_model_profile_choices` ([reference](reference/tools/system/list_model_profile_choices)):

```json
{"toolset_id":"system","tool_name":"get_agent","arguments":{"id":"operator"}}
```

```json
{"toolset_id":"system","tool_name":"list_model_profile_choices","arguments":{"limit":200}}
```

Expected: an agent the caller may use, with `model.profile_id` naming a usable profile choice. Page the choices if necessary. Session creation binds an existing agent; it does not take a model profile argument. If the agent/profile is missing, stop before creating anything; [agents/](agents) explains the binding and [runbooks/configure-provider](runbooks/configure-provider) is the admin configuration path. These reads alone do not prove the model endpoint is reachable; the session result verifies execution.

3. List the visible workspaces with `workspaces__list_workspaces` ([reference](reference/tools/workspaces/list_workspaces)):

```json
{"toolset_id":"workspaces","tool_name":"list_workspaces","arguments":{"limit":20}}
```

Expected: readable rows. Page through them using the returned pagination. Identify the caller's own default workspace by `is_default: true`, never by a name such as `prime`. Choose an unused DNS-label ID for the separate workspace; a concurrent create can still answer `type=conflict`.

## Roles and gates
- runner: `user`. Reading a template needs `read` access. Workspace/file/log/session reads need `read`; writing files and starting sessions need `full`; deletion needs `owner`. No access answers `not-found`, while a visible row with insufficient access answers `forbidden`. Admin privileges do not make a default workspace deletable.
- the shipped approval gates below are all `none`. Role/access requirements still apply. Before the first write, read the live policy of every planned write with `system__describe_tool` ([reference](reference/tools/system/describe_tool)):

| scoped write | shipped gate | who approves / card |
|---|---|---|
| `workspaces__create_workspace` | none | no shipped approval card |
| `workspaces__write_workspace_file` | none | no shipped approval card |
| `workspaces__create_workspace_session` | none | no shipped approval card |
| `workspaces__delete_workspace` | none | no shipped approval card |

```json
{"toolset_id":"system","tool_name":"describe_tool","arguments":{"toolset_id":"workspaces","tool_name":"create_workspace"}}
```

```json
{"toolset_id":"system","tool_name":"describe_tool","arguments":{"toolset_id":"workspaces","tool_name":"write_workspace_file"}}
```

```json
{"toolset_id":"system","tool_name":"describe_tool","arguments":{"toolset_id":"workspaces","tool_name":"create_workspace_session"}}
```

```json
{"toolset_id":"system","tool_name":"describe_tool","arguments":{"toolset_id":"workspaces","tool_name":"delete_workspace"}}
```

Expected: current schemas, role floors and live approval policies. An operator may add policies after installation. For an in-platform agent, a live gate parks the turn at that write until a person decides; do not retry or work around the gate. External MCP cannot park a turn: a required-gated tool is unavailable through that endpoint, so stop at the precondition boundary above. Re-read its policy before a later write if the flow was interrupted or configuration changed.

## Steps
### 1. Create and read the workspace
Call `workspaces__create_workspace` ([reference](reference/tools/workspaces/create_workspace)):

```json
{"toolset_id":"workspaces","tool_name":"create_workspace","arguments":{"id":"w7-workspace","template_id":"local-default"}}
```

Expected: the stored workspace row with the actual ID and template snapshot, initially `phase=pending`. It is usable immediately; the first successful health probe promotes it to `running`. When creation has a known user, that creator is stamped in `access.owners`; an auth-disabled or otherwise unattributed call is not proof of a real user's ownership. Read back with `workspaces__get_workspace` ([reference](reference/tools/workspaces/get_workspace)):

```json
{"toolset_id":"workspaces","tool_name":"get_workspace","arguments":{"id":"w7-workspace"}}
```

Verify the ID, template/provider, `template_snapshot`, phase and, for a known user, ownership. `phase=failed` and `failure_reason` require investigation; a successful create is not evidence that later health checks passed.

### 2. Browse, write and read a file
Call `workspaces__list_workspace_files` ([reference](reference/tools/workspaces/list_workspace_files)):

```json
{"toolset_id":"workspaces","tool_name":"list_workspace_files","arguments":{"workspace_id":"w7-workspace","path":".","recursive":false,"limit":20}}
```

Expected: `{items, offset, length, total, path}`. Page with `offset`. A recursive listing can return `truncated: true`; its `total` is then a lower bound. Narrow the path or page further, and report any remaining truncation. Do not inspect reserved `.state` or `.tmp` trees to infer session state.

After confirming the destination may be created or overwritten, call `workspaces__write_workspace_file` ([reference](reference/tools/workspaces/write_workspace_file)):

```json
{"toolset_id":"workspaces","tool_name":"write_workspace_file","arguments":{"workspace_id":"w7-workspace","path":"notes/welcome.txt","content":"Workspace ready.\n","encoding":"text"}}
```

Expected: the path and UTF-8 byte count. This replaces an existing file; parent directories are created as needed. Read the bytes back with `workspaces__read_workspace_file` ([reference](reference/tools/workspaces/read_workspace_file)):

```json
{"toolset_id":"workspaces","tool_name":"read_workspace_file","arguments":{"workspace_id":"w7-workspace","path":"notes/welcome.txt","encoding":"text"}}
```

Verify `content` is exactly `Workspace ready.` followed by a newline, and `size_bytes` is 17. Binary files use `encoding: base64`; do not treat a UTF-8 decoding refusal as an absent file.

### 3. Read the history for its actual purpose
Call `workspaces__get_workspace_log` ([reference](reference/tools/workspaces/get_workspace_log)):

```json
{"toolset_id":"workspaces","tool_name":"get_workspace_log","arguments":{"workspace_id":"w7-workspace","limit":10}}
```

Expected: `{commits: [...]}`, newest first, from the workspace's state-repo history with attribution trailers. This is not the session's lifecycle state or a promise that a standalone file write created a commit. The file readback above proves the file's contents.

### 4. Start and observe the session
Call `workspaces__create_workspace_session` ([reference](reference/tools/workspaces/create_workspace_session)) with the verified agent binding:

```json
{"toolset_id":"workspaces","tool_name":"create_workspace_session","arguments":{"workspace_id":"w7-workspace","binding":{"kind":"agent","agent_id":"operator"},"initial_instructions":"Read notes/welcome.txt and report its contents. Do not write files.","auto_start":true}}
```

Expected: a created session with an ID. Save that returned ID; `w7-session` below is only a worked example. Read it with `workspaces__get_workspace_session` ([reference](reference/tools/workspaces/get_workspace_session)):

```json
{"toolset_id":"workspaces","tool_name":"get_workspace_session","arguments":{"workspace_id":"w7-workspace","session_id":"w7-session"}}
```

Expected: `{info, status}` for that session, with the same workspace and binding. Poll while it is running; inspect its answer through [sessions/observe-a-session](sessions/observe-a-session) and confirm it reports the file's actual content. A `waiting` lifecycle status alone is not proof of success or of an approval wait: [runbooks/workspace-status](runbooks/workspace-status) explains derived state, truncation and park reasons. Stop and report a failed model connection rather than claiming the agent ran successfully.

### 5. Optional destruction, after the user's agreement
Keep the workspace if the user wants to continue. Before destruction, verify the exact target ID, that it is not the caller's default, and that needed files/transcripts have been saved elsewhere. Default workspaces, including prime and every user's private default, are protected. Do not test the refusal by attempting to delete a live default workspace.

Call `workspaces__delete_workspace` ([reference](reference/tools/workspaces/delete_workspace)) only for the agreed separate workspace:

```json
{"toolset_id":"workspaces","tool_name":"delete_workspace","arguments":{"id":"w7-workspace"}}
```

Expected: `{deleted: true, id}`. Deletion is synchronous; concurrent reads show `phase=terminating`. The normal path destroys the runtime/resources and files, ends open sessions with `workspace_lost`, purges their rows/history, then removes the workspace row. It is workspace destruction, not template retirement. On a local provider explicitly refused by deployment policy, deletion instead abandons the row and sessions while leaving provider files for an operator; report that exception rather than promising those files were destroyed.

## Verification
- Before optional deletion: the durable workspace read matches its creation ID, template snapshot and known user's ownership; file readback matches all bytes; session readback matches its binding and the observed answer reports the file content. Record the live gate responses used for the writes. A 200 response alone is insufficient.
- After deletion, repeat `workspaces__get_workspace` with the same ID: expected `type=not-found`. Enumerate the visible workspaces again, including all pages: the deleted ID is absent and the caller's default is still present. The final read is `workspaces__list_workspaces`:

```json
{"toolset_id":"workspaces","tool_name":"list_workspaces","arguments":{"limit":20}}
```

## Refusal and failure
| result | meaning and next move |
|---|---|
| `code=tool_unavailable` (external MCP) | The tool is not available to this caller through this endpoint. Stop and report the precondition failure; an operator checks exposure and access without inferring whether a hidden tool exists. |
| `type=approval_required` | Parked on a live policy; a person decides; do not retry the write. |
| `type=not-found` | Unknown or unreadable template/workspace/agent/session, or a removed provider. Recheck the named precondition; no hidden resource is disclosed. After successful deletion, this is the expected readback. |
| `type=forbidden` | Insufficient access, admin-only secret overrides, or an attempt to delete a visible default workspace. Stop; a default refusal tears nothing down and ends no session. |
| `type=conflict` | Workspace ID already exists, including a concurrent create. Read the existing row; do not overwrite it or delete it as cleanup. Choose a new ID only for a new workspace. |
| `type=validation-error` | Invalid ID, unsupported resource override, masked secret reuse or invalid session binding. Follow the live schema and [workspaces/templates](workspaces/templates); nothing is created by a request refused before materialisation. |
| `type=bad-request` | Invalid encoding/path, reserved file tree, or a file backend error. Use text or base64 appropriately; keep paths inside the workspace. |
| `type=not-implemented` | A Kubernetes `agent_sandbox` provider is unsupported. Ask an admin for a supported template. |
| `type=unavailable` | Session services are not wired. Stop and report deployment configuration. |
| `type=backend-error` during create or execution | Provider/runtime failure or local-workspace policy refusal. Read the error and the workspace's phase/reason if a row exists; do not claim a runnable session. |
| `type=backend-error` during deletion | Partial teardown leaves `phase=failed` and a `failure_reason` explaining that deletion did not finish; open sessions are ended `workspace_lost`. Report the loss and finish the same agreed deletion after the cause is resolved. If the provider cannot be resolved before teardown starts, nothing is torn down and the previous phase is restored. Do not confuse this with an approval retry. |

## Related
- [purpose-map](purpose-map) and [runbooks/conventions](runbooks/conventions): the index and authoring contract.
- [workspaces/create-and-delete](workspaces/create-and-delete), [workspaces/templates](workspaces/templates) and [workspaces/prime](workspaces/prime): lifecycle, recipes and default-workspace protection.
- [workspaces/files-and-log](workspaces/files-and-log) and [sessions/start-a-session](sessions/start-a-session): file semantics and session bindings.
- [runbooks/workspace-status](runbooks/workspace-status): status across visible workspaces and sessions.
