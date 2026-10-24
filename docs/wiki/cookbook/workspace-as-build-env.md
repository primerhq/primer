---
title: "A workspace as a build environment"
summary: "Use a throwaway workspace as a reproducible build, run a build agent in it and collect logs and artifacts."
order: 4
tools: [workspaces__create_workspace_template, system__create_agent, workspaces__create_workspace, workspaces__create_workspace_session, workspaces__get_workspace_session, workspaces__get_workspace_log, workspaces__read_workspace_file, workspaces__delete_workspace]
related: [workspaces/templates, workspaces/files-and-log]
---
## Goal

Use a throwaway workspace as a reproducible build environment: write the build recipe as a template, run a build agent in a fresh instance, collect the result file and the state log, then delete the instance.

## Prerequisites

- Run it over MCP or from a session in the default workspace (prime). From a session in any other workspace, `workspaces__create_workspace` answers `type=forbidden`, and the workspace tools reach only that session's own workspace.
- A container workspace provider. `workspaces__list_workspace_provider_choices` lists the provider ids and kinds you may use.
- A build image that contains the primer workspace runtime: build it FROM `primer/workspace-runtime:1.1` with the toolchain, as step 1 shows.
- A model profile for the agent. The `profile_id` is in [model profiles](providers/model-profiles).

## Steps

### 1. Create the build-env template

Create the materialisation recipe with `workspaces__create_workspace_template`:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_template", "arguments": {"entity": {"id": "rust-build", "description": "Rust 1.83 toolchain on the workspace runtime, 4 cores, 8 GiB", "provider_id": "docker-1", "backend": {"kind": "container", "image": "example.com/build-rust:1.83", "cpu_cores": 4, "memory_bytes": 8589934592}}}}
```

Response:

```json
{"id": "rust-build", "description": "Rust 1.83 toolchain on the workspace runtime, 4 cores, 8 GiB", "provider_id": "docker-1", "backend": {"kind": "container", "image": "example.com/build-rust:1.83", "cpu_cores": 4, "memory_bytes": 8589934592}}
```

The image must contain the primer workspace runtime: build it FROM `primer/workspace-runtime:1.1` with the toolchain. Thread the template's `id` into the next step.

### 2. Create the build agent

Create the agent with `system__create_agent`:

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "rust-builder", "description": "Clones a repo, runs cargo test, writes the result to build-result.txt", "system_prompt": ["Clone the repo at the given URL, check out the given SHA, run cargo test, and write the full test output plus a pass/fail line to build-result.txt in the workspace root."], "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "tools": []}}}
```

Response:

```json
{"id": "rust-builder", "description": "Clones a repo, runs cargo test, writes the result to build-result.txt", "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "tools": []}
```

The workspace's own shell and file tools are injected by the binding, so `tools` stays empty: it lists non-workspace tools only. Omit the `id` and the server assigns one. Thread the agent's `id` into the next step.

### 3. Materialise the workspace

Create the live workspace from the template with `workspaces__create_workspace`:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"template_id": "rust-build"}}
```

Response:

```json
{"id": "ws-4c81", "template_id": "rust-build", "phase": "pending"}
```

`phase` is `pending` at the answer: the row starts `pending` and is usable at once (start the build session in it now). The health probe turns it `running` at its first good probe, about 30 seconds later, and `failed` after three missed probes. Instances do not pick up later template edits: recreate the instance after you change the template. Thread the workspace's `id` into the next step.

### 4. Start the build session

Bind the agent and start the build with `workspaces__create_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "ws-4c81", "binding": {"kind": "agent", "agent_id": "rust-builder"}, "initial_instructions": "repo=https://example.com/my-crate sha=abc123", "autonomous": true, "auto_start": true}}
```

Response:

```json
{"id": "sess-7d2e", "workspace_id": "ws-4c81", "status": "running"}
```

`autonomous: true` ends the session on its clean turn. Without it, the session rests `waiting` after the turn, and only a new message or a steer starts the next one. Thread the session's `id` into the next step.

### 5. Poll until the build ends

Read the session state with `workspaces__get_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-4c81", "session_id": "sess-7d2e"}}
```

Response:

```json
{"info": {"session_id": "sess-7d2e", "status": "ended", "ended_reason": "completed", "ended_detail": null}, "status": "ended"}
```

Poll while `status` is `running` or `waiting`. `ended` is the stop condition: `info.ended_reason` is `completed` for a clean exit, `failed` for an error and `cancelled` when you asked for it.

### 6. Read the state log

Read the workspace's state-repo history with `workspaces__get_workspace_log`:

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_log", "arguments": {"workspace_id": "ws-4c81"}}
```

Response:

```json
{"commits": [{"sha": "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432", "subject": "status[sess-7d2e]: -> ended", "committed_at": "2026-10-24T14:05:11Z", "session_id": "sess-7d2e", "agent_id": "rust-builder", "op": "status_change"}]}
```

Newest first, up to `limit` commits (50 by default). Each commit records a change to the workspace's state, its transcript included, and its trailers name the session, the agent, the `op` and the `tool` call that made it. The log is not the artifact store: the files the build wrote live in the workspace, and step 7 reads one back.

### 7. Read the result artifact

Read the result back from the workspace with `workspaces__read_workspace_file`:

```json
{"toolset_id": "workspaces", "tool_name": "read_workspace_file", "arguments": {"workspace_id": "ws-4c81", "path": "build-result.txt"}}
```

Response:

```json
{"path": "build-result.txt", "encoding": "text", "content": "test result: ok. 12 passed; 0 failed", "size_bytes": 36}
```

A passing build ends with a `test result: ok.` line.

### 8. Delete the workspace

Tear the workspace down and free its backend resources with `workspaces__delete_workspace`:

```json
{"toolset_id": "workspaces", "tool_name": "delete_workspace", "arguments": {"id": "ws-4c81"}}
```

Response:

```json
{"deleted": true, "id": "ws-4c81"}
```

While the delete runs the workspace reads `phase: terminating`. A teardown that fails part way (`type=backend-error`) leaves it `phase: failed` with a `failure_reason` that says the delete did not finish: delete it again to finish. Deleting a workspace ends every open session in it, and it is refused (`type=forbidden`) while one of them runs at a higher rank than you: end the session first.

## Verify

`workspaces__get_workspace_session` reads `status: ended` with `ended_reason: completed`, `workspaces__get_workspace_log` shows the build session's commits, `workspaces__read_workspace_file` returns the result file, and `workspaces__delete_workspace` answered `deleted: true`.

## Gotchas

- The template's `cpu_cores` and `memory_bytes` are the workspace's caps, not the agent's. A build that needs more than the template allows can fail: size the template for the heaviest build you run.
- A bare toolchain image, like `rust:1.83-slim`, does not contain the workspace runtime, so step 3 fails after 30 seconds with `type=backend-error` and nothing is left behind. Build the image FROM `primer/workspace-runtime:1.1` instead.
- For repeatable builds, pin the toolchain in the repo via `rust-toolchain.toml` rather than relying on the base image version.

## Related

- [Workspace templates](workspaces/templates)
- [Files and the log](workspaces/files-and-log)
