---
title: "Create and delete a workspace"
summary: "Create a workspace from a template, look it up, list them, and delete one (which destroys its files and ends its sessions)."
order: 2
tools: [workspaces__list_workspaces, workspaces__get_workspace, workspaces__create_workspace, workspaces__delete_workspace]
related: [workspaces/templates, workspaces/prime, sessions/start-a-session]
---
## What it is
`workspaces__create_workspace` builds a live workspace from a template. `workspaces__list_workspaces` and `workspaces__get_workspace` look workspaces up. `workspaces__delete_workspace` destroys one, with its files, and ends every session in it.

## Concepts
- `template_id` is required. The provider comes from the template: there is no `provider_id` argument.
- `id` is optional. When you give one, it must be a DNS label: lowercase letters, digits and `-`, starting and ending with a letter or digit, at most 63 characters. An id already in use is a `conflict`. Without one, the platform picks the id; read it from the answer.
- `overrides` change the template for this one workspace:
  - `env` keys are added to the template's, and yours win on a clash.
  - `files` and `init_commands` run after the template's own.
  - `resources` sizes this workspace: `cpu_cores`, `memory_bytes` and `disk_bytes`. Kubernetes takes all three, Docker takes `cpu_cores` and `memory_bytes`, and Local takes none. A size the provider cannot take is a `validation-error` that names `overrides.resources.<field>`, and nothing is created. The ceilings are 1024 cores and 64 TiB of memory or disk.
  - An env name follows the Kubernetes rule (`^[-._a-zA-Z][-._a-zA-Z0-9]*$`, at most 256 characters). `PRIMER_RUNTIME_TOKEN` and `RUNTIME_TOKEN` are reserved. A bad name is a `validation-error` that names it.
  - A file whose source `kind` is `secret` needs an admin, in `overrides.files` as in a template.
- The answer is the stored row. A row this tool creates starts `pending` and turns `running` at its first good probe: [what a workspace is](workspaces/what-a-workspace-is).
- Every row that `list_workspaces` and `get_workspace` serve carries `is_default`, which is `true` on the default workspace (prime) only.
- The reach rule:
  - From a session outside prime, `create_workspace` is refused with `forbidden` before anything is looked up, so the answer confirms neither the template nor the id.
  - From a session outside prime, `list_workspaces` lists only your own workspace, and `get_workspace` and `delete_workspace` reach only your own. Any other id, prime included, answers `not-found`, exactly like an id that does not exist.
  - From a session in prime, and with no calling session (a person or an API client over MCP), you reach every workspace.
- A delete is synchronous. It tears down the live instance (on Docker the container and its volume, on a local provider the workspace folder, on Kubernetes the pod's objects and its volume claim), ends every open session in the workspace with `workspace_lost`, which is permanent, and deletes the row. The files and every session's history go with it.
- A delete follows the rank rule. It ends every open session in the workspace, so it applies the cancel rule to each: while one of them runs at a higher rank than you, the delete answers `forbidden` and nothing is torn down. An admin is never outranked. With no calling session, a session your own account started never blocks you.
- The default workspace (prime) is never deleted. The tool answers `forbidden`, and the REST route answers 403 with `reserved_workspace`. Nothing is torn down and no session ends. See [the prime workspace](workspaces/prime).
- A workspace on a local provider can be refused. When the operator has told a deployment whose API and workers run in separate processes to refuse local workspaces, a create on a local template fails and creates nothing (REST answers 409 `/errors/workspace-refused`). Use a template on a Docker or Kubernetes provider instead.

## How to
1. Pick a template: list them with `workspaces__list_workspace_templates` ([workspace templates](workspaces/templates)).
2. Create the workspace from a session in prime, or with no calling session. Give an `id` when you want a name you can type.
3. Keep the id from the answer and start your sessions there: [start a session](sessions/start-a-session).
4. Before you delete, ask the user. A delete destroys the files and every transcript, and it cannot be undone.

## Tool calls
### Create a workspace
Builds the workspace from the template, with one extra environment variable for this workspace only.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"id": "invoice-parser", "template_id": "local-default", "overrides": {"env": {"LOG_LEVEL": "debug"}}}}
```

Reference: [`workspaces__create_workspace`](reference/tools/workspaces/create_workspace)

### Size one workspace
Gives a Kubernetes workspace its own CPU, memory and disk; on Docker leave out `disk_bytes`.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"id": "invoice-parser-big", "template_id": "k8s-python", "overrides": {"resources": {"cpu_cores": 4, "memory_bytes": 8589934592, "disk_bytes": 21474836480}}}}
```

Reference: [`workspaces__create_workspace`](reference/tools/workspaces/create_workspace)

### List workspaces
Pages through the workspaces you reach; the default one carries `is_default: true`.

```json
{"toolset_id": "workspaces", "tool_name": "list_workspaces", "arguments": {"limit": 50}}
```

Reference: [`workspaces__list_workspaces`](reference/tools/workspaces/list_workspaces)

### Get a workspace
Reads one row: its `phase`, `failure_reason`, provider and template snapshot.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace", "arguments": {"id": "invoice-parser"}}
```

Reference: [`workspaces__get_workspace`](reference/tools/workspaces/get_workspace)

### Delete a workspace
Destroys the workspace, its files and its sessions' history, and answers `{deleted: true, id}`.

```json
{"toolset_id": "workspaces", "tool_name": "delete_workspace", "arguments": {"id": "invoice-parser"}}
```

Reference: [`workspaces__delete_workspace`](reference/tools/workspaces/delete_workspace)

## Gotchas
- `create_workspace` from a session outside prime is always `forbidden`. Ask the user, or prime's operator, to create the workspace.
- Never find prime by its name: any workspace may be called `prime`. Look for `is_default: true`.
- A template id that does not exist is a `not-found`; an id already taken is a `conflict`.
- A Docker template's image must contain the primer workspace runtime. If the runtime does not answer within 30 seconds, the create fails with `backend-error`, the message names the image, and nothing is left behind.
- A delete refused with `forbidden` tore nothing down: the workspace and its sessions are as they were.
- While a delete runs, a new session in that workspace is refused.
- From a session outside prime you can delete your own workspace, and that ends your own session with the rest. Ask the user first, every time.

## Related
[workspace templates](workspaces/templates), [the prime workspace](workspaces/prime), [start a session](sessions/start-a-session).
