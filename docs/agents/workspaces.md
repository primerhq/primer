---
slug: workspaces
title: Workspaces - execution sandboxes
summary: Isolated filesystem + git state for sessions, with multi-backend providers, templates, and a fixed .state/.tmp layout.
related: [sessions, agents, knowledge]
mcp_tools:
  - system::list_workspaces
  - system::get_workspace
  - system::create_workspace
  - system::update_workspace
  - system::delete_workspace
  - system::find_workspaces
  - system::list_workspace_providers
  - system::list_workspace_templates
  - workspaces::list_workspace
  - workspaces::get_workspace
  - workspaces::create_workspace
  - workspaces::delete_workspace
  - workspaces::read_workspace_file
  - workspaces::write_workspace_file
  - workspaces::watch_files
---

# Workspaces - execution sandboxes

## Overview

A **Workspace** is primer's unit of execution isolation. Concretely,
it's a filesystem (local directory, container volume, or Kubernetes
PVC depending on the backing provider) plus a git repository on top
of it that tracks state changes. Sessions and chat turns running on
behalf of an agent operate inside one workspace; the workspace is
their entire world for filesystem and command-execution purposes.

The reason workspaces exist is twofold. First, isolation: each
workspace's files are separate from every other workspace's files,
so two agent runs don't accidentally clobber each other. Second,
provenance: every state-changing tool call commits to the workspace's
git repo, so the operator can replay or audit "what did this agent
actually change" turn by turn.

Workspaces are stable across sessions. The same workspace can host
many sessions over its lifetime - a long-lived workspace for an
ongoing customer engagement might see dozens of sessions, each
modifying the shared filesystem in turn. Multi-session coordination
on a shared workspace is by filesystem (write a file in
`.state/shared/`, read it from the next session).

## Mental model

Three rows make up the workspace surface:

- `WorkspaceProvider` - the abstract compute substrate. Local
  (filesystem path on the host), Docker (container-backed),
  Kubernetes (pod-backed). Reserved provider ids exist for the
  built-in substrates; operators can register additional providers
  for custom backends.
- `WorkspaceTemplate` - a reusable workspace config. Bundles a
  provider, a default file layout, default env vars, default tool
  set. Workspaces created from a template start with the template's
  config; subsequent edits diverge.
- `Workspace` - the live instance. `id`, `provider_id`,
  `template_id` (nullable), `status`. The actual filesystem +
  state repo are bound to this row.

The fixed directory layout under each workspace:
- (workspace root) - files the agent reads and writes; arbitrary
  layout.
- `.state/` - the git-tracked state repo. Per-session subtree at
  `.state/sessions/<sid>/` (LLM message history as commits).
  Shared subtree at `.state/shared/` (cross-session coordination).
- `.tmp/` - non-tracked scratch. Per-session at `.tmp/<sid>/` (tool
  output overflow cache). Cleared on workspace delete.

The agent reaches the workspace through workspace tools (`ls`,
`read`, `write`, `edit`, `glob`, `grep`, `exec`). These tools are
composed onto the agent at session start with the workspace
already bound - they don't take a `workspace_id` arg; they
operate on "the workspace this session is in."

External access via MCP is different. The `workspaces::*` tools
take an explicit `workspace_id` because they're called from
contexts that have no implicit session. They expose the same
operations (list / get / create / delete workspaces; read / write
files; watch for file changes) so an external MCP-connected agent
can drive workspace state directly.

## Lifecycle and states

A `Workspace.status` is one of:

- `provisioning` - the backing substrate is being created (cloning
  a pod, mounting a volume). Transient; can be observed.
- `ready` - operational. Sessions can be created against it.
- `error` - provision failed; details in the `error` field.
- `terminating` - operator-requested teardown; backing substrate
  being destroyed.
- `terminated` - substrate is gone. The row stays in storage as an
  audit record; can be deleted manually.

Provisioning is async (claim-based, like sessions and harnesses).
Workspace create returns 202; poll `system::get_workspace` until
`status=ready`.

Sessions run inside a workspace. A workspace destroyed while
sessions are active cascades to cancel them. The cancel is
async - sessions get a `cancelled` marker, parked tools get
`YieldCancelled` payloads, the workspace then transitions to
`terminated`.

## MCP tools

Two surfaces. The system toolset has generic CRUD; the workspaces
toolset has the ergonomic verb-style operations + file I/O.

### Generic CRUD (system toolset)

- `system::list_workspaces` - paginated.
- `system::get_workspace` - fetch by id.
- `system::create_workspace` - body needs `id`, `provider_id`,
  optional `template_id`. Returns 202.
- `system::update_workspace` - partial update of metadata.
- `system::delete_workspace` - cascade-cancels in-flight sessions.
  Returns 202.
- `system::find_workspaces` - predicate query.
- `system::list_workspace_providers` - list registered providers.
- `system::list_workspace_templates` - list templates.

### Workspace-aware tools (workspaces toolset)

- `workspaces::list_workspace` - paginated; same as system list.
- `workspaces::get_workspace` - same as system get.
- `workspaces::create_workspace` - same as system create.
- `workspaces::delete_workspace` - same as system delete.
- `workspaces::read_workspace_file` - read file from a workspace
  by relative path. Body: `workspace_id`, `path`. Returns text
  content. 404 on missing file.
- `workspaces::write_workspace_file` - write file. Body:
  `workspace_id`, `path`, `content`, optional `mode`
  ("0644" etc.). Refuses to overwrite a file the current session
  hasn't read first (only relevant inside a session context;
  external MCP callers always force-write).
- `workspaces::watch_files` - yielding tool. NOT exposed over MCP.
  Inside a session, yields until a watched file changes.

The `workspaces::*` toolset is hidden from MCP for any tool that
needs an active `AgentSession` (`watch_files`). The file read/write
tools are MCP-exposable because they take an explicit
`workspace_id` and don't rely on session context.

## Workflows

### Workflow 1 - external agent inspects a workspace's files

**Goal.** Connected over MCP, find a workspace and read its
manifest.

1. List workspaces:

```json
{
  "tool": "workspaces::list_workspace",
  "arguments": {"limit": 100}
}
```

2. Get details of `ws-target`:

```json
{
  "tool": "workspaces::get_workspace",
  "arguments": {"id": "ws-target"}
}
```

Returns the row with `status`, `provider_id`, etc.

3. Read the manifest:

```json
{
  "tool": "workspaces::read_workspace_file",
  "arguments": {
    "workspace_id": "ws-target",
    "path": "manifest.yaml"
  }
}
```

Returns the file contents.

### Workflow 2 - write a coordination file from one session for
another to pick up

**Goal.** Session A drops a file in `.state/shared/` that a later
session B can read.

Inside session A (using session-bound tools, not the MCP `workspaces`
toolset):

```json
{
  "tool": "write",
  "arguments": {
    "path": ".state/shared/handoff.json",
    "content": "{\"next_action\": \"deploy\", \"branch\": \"main\"}"
  }
}
```

This commits to the workspace's `.state` repo. Session B
(spawned later in the same workspace) can `read` the same path
and pick up the handoff.

## Gotchas

- **The workspace tools an agent has depend on the session.**
  Inside a session, `read`, `write`, `exec` (etc.) are present
  without `workspace_id` - they're bound. From MCP, the
  `workspaces::*` toolset has analogous tools that take
  `workspace_id` explicitly.
- **`.state/` is git-tracked; `.tmp/` is not.** Commits in `.state/`
  outlive sessions. Files in `.tmp/` get cleared on workspace
  delete (and never persist across restarts on some backends).
- **Tool output caching uses `.tmp/`.** Outputs over threshold get
  written to `.tmp/<sid>/`, agent sees preview + read-this hint.
  Code that expects full tool output inline gets surprised.
- **Read-before-write enforcement.** Inside a session, `write` to
  an existing file refuses unless the session has previously
  `read` it (or unless `force=True`). This is to prevent
  agents from clobbering files they haven't seen. External MCP
  writes via `workspaces::write_workspace_file` always force-
  write (no session-read tracking outside a session).
- **Multi-session shared filesystem is collaborative, not
  isolated.** Two simultaneous sessions on the same workspace
  WILL race on file writes. Coordinate via `.state/shared/`.
- **Workspace delete is async.** The 202 returns immediately;
  the actual teardown can take seconds (local) to minutes
  (k8s pod). Sessions on the workspace get cancelled in the
  process.
- **Provisioning errors leave `status=error`.** Don't try to use
  an erroring workspace - recreate with a fresh id.
- **`watch_files` is invisible from MCP.** It's a yielding tool;
  the MCP exposability gate drops it. External agents wanting
  change-detection should poll `read_workspace_file` instead.

## Related

- [sessions](sessions.md) - sessions live inside workspaces.
- [agents](agents.md) - agents define how a session uses the
  workspace.
- [knowledge](knowledge.md) - when knowledge content is stored
  as files in a workspace, the workspace toolset is how the agent
  reads it. (Collections are the alternative for
  reuse-across-workspaces.)
