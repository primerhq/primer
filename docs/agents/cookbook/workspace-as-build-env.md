---
slug: cookbook/workspace-as-build-env
title: Workspace As A Build Environment
summary: Use an ephemeral workspace as a one-shot reproducible build environment, run a build agent in it, and collect the logs and artifacts.
mcp_tools:
  - workspaces::create_workspace_template
  - system::create_agent
  - workspaces::create_workspace
  - workspaces::create_workspace_session
  - workspaces::get_workspace_session
  - workspaces::get_workspace_log
  - workspaces::read_workspace_file
  - workspaces::delete_workspace
---

## Goal
Treat a workspace as a disposable build environment. Create a template carrying a Rust toolchain and the build tools, materialise an instance, run a build agent that clones a repo and runs `cargo test`, then collect the result file and the git-backed state log.

## Prerequisites
- A Docker (or other container) workspace provider registered, with the `rust:1.83-slim` image reachable from the provider's daemon.
- Permission to create workspace templates, agents, workspaces, and sessions over MCP.

## Steps
### 1. Create the build-env template
`workspaces::create_workspace_template`
```json
{
  "entity": {
    "id": "rust-1.83",
    "description": "Rust 1.83 toolchain build env, 20m TTL",
    "provider_id": "docker-1",
    "backend": { "kind": "container", "image": "rust:1.83-slim" },
    "env": { "CARGO_TERM_COLOR": "never" },
    "init_commands": ["apt-get update && apt-get install -y git"]
  }
}
```
Response:
```json
{ "id": "rust-1.83" }
```
`CARGO_TERM_COLOR=never` keeps test output plain text so the agent reads it cleanly. The `init_commands` install `git` once at provision time. Set the TTL just longer than your heaviest typical build: too short and the workspace is torn down mid-build, too long and idle workspaces accumulate.

### 2. Create the build agent
`system::create_agent`
```json
{
  "entity": {
    "id": "rust-builder",
    "description": "Clones a repo, runs cargo test, reports pass/fail",
    "system_prompt": ["Clone the repo at the given URL, check out the given SHA, run cargo test, and write the full test output plus a pass/fail line to build-result.txt in the workspace root."],
    "model": { "provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6" }
  }
}
```
Response:
```json
{ "id": "rust-builder" }
```
Bind the shell and filesystem tools from the system toolset so the agent can run `cargo` and write files. A mid-tier model is sufficient for a deterministic build script.

### 3. Materialise a workspace instance
`workspaces::create_workspace`
```json
{ "template_id": "rust-1.83" }
```
Response:
```json
{ "id": "ws-1", "phase": "running" }
```
Thread `id` ("ws-1") into the session call. Wait until `phase` is `running`. Instances do not pick up later template edits; recreate the instance after changing the template.

### 4. Start the build session
`workspaces::create_workspace_session`
```json
{
  "workspace_id": "ws-1",
  "binding": { "kind": "agent", "agent_id": "rust-builder" },
  "initial_instructions": "repo=https://github.com/example/my-crate sha=abc123",
  "auto_start": true
}
```
Response:
```json
{ "id": "ses-1", "status": "running" }
```
Pass the repo URL and commit SHA as the session input. Thread `id` ("ses-1") as `session_id`.

### 5. Poll until the build ends
`workspaces::get_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "ended", "ended_reason": "completed" }
```
Re-call on an interval until `status` is `ended`.

### 6. Collect the result artifact
`workspaces::read_workspace_file`
```json
{ "workspace_id": "ws-1", "path": "build-result.txt" }
```
Response:
```json
{ "path": "build-result.txt", "content": "test result: ok. 12 passed; 0 failed" }
```
A passing build ends with a `test result: ok.` line.

### 7. Collect the build log
`workspaces::get_workspace_log`
```json
{ "workspace_id": "ws-1" }
```
Response:
```json
{ "entries": [ { "commit": "a1b2c3", "message": "agent write build-result.txt" } ] }
```
The git-backed state log records a commit per agent write, giving a timestamped record of every artifact the build touched.

## Verify
`status` is `ended` with `ended_reason: "completed"`, `build-result.txt` exists and contains the `cargo test` output, and the workspace log shows a commit per write. Delete the workspace when done with `workspaces::delete_workspace` `{ "id": "ws-1" }`.

## Gotchas
- Resource caps (CPU, memory) come from the template, not the agent. A build that needs more memory than the template allows can fail with truncated output. Match the caps to the heaviest crate you build.
- Workspace network egress is provider-dependent: the local provider has full egress, the container backend follows the daemon network config, and Kubernetes follows the NetworkPolicy.
- For repeatable builds, pin the toolchain in the repo via `rust-toolchain.toml` rather than relying on the base image version.

## Related
- `workspaces`, `agents`, `sessions`
- `cookbook/create-and-run-a-session`
- `cookbook/run-a-graph-and-collect-results`
