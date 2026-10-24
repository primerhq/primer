---
title: "Workspace templates"
summary: "Reusable workspace recipes: list, read, create, change and delete templates, and which settings need an admin."
order: 4
tools: [workspaces__list_workspace_templates, workspaces__get_workspace_template, workspaces__create_workspace_template, workspaces__update_workspace_template, workspaces__delete_workspace_template, workspaces__list_workspace_provider_choices]
related: [workspaces/create-and-delete, workspaces/providers]
---
## What it is
A workspace template (`WorkspaceTemplate`) is a reusable recipe: which provider builds the workspace and what goes into it. You build workspaces from it with `workspaces__create_workspace` ([create and delete a workspace](workspaces/create-and-delete)). Every template tool is open to users; a few settings inside a template need an admin.

## Concepts
- A template holds: `id` (optional on create; the platform picks one), `description` (required), `provider_id`, `backend` (it must match the provider's kind), `files`, `env`, `init_commands`, `resources`, and rarely `state_path`, `tmp_path` and `strict_write_locking`.
- Finding a `provider_id`: `workspaces__list_workspace_providers` needs an admin, because a provider row holds its connection. Anyone may call `workspaces__list_workspace_provider_choices`: it answers `items` of `{id, kind, label, default_image}`, sorted by id, and never a provider's configuration.
- The `backend` block, by kind:
  - Local (`{"kind": "local"}`) runs on the server itself, in the provider's root folder. It has no image and no size.
  - Docker (`"kind": "container"`) takes `backend.image` (`primer/workspace-runtime:1.1` when omitted), `backend.cpu_cores` and `backend.memory_bytes`. It has no disk setting. A custom image must be built `FROM primer/workspace-runtime:1.1`.
  - Kubernetes (`"kind": "kubernetes"`) requires `backend.image`. `backend.entrypoint` is optional (the pod starts `["python", "-m", "primer_runtime.server"]` without one) and `backend.args` is appended. The size is `resources.cpu_cores` and `resources.memory_bytes` (requests equal limits), the disk is `backend.pvc_size`, a quantity such as `10Gi`, and the placement fields are `node_selector`, `tolerations`, `gpu` (`{count, resource_name}`, a count of 1 to 64), `service_account_name` and `image_pull_policy` (`Always`, `IfNotPresent` or `Never`).
- `files` seed the workspace. Each has a `path`, a `source` and an optional octal `mode`. The source `kind` is `inline` (`content`), `url` (`url`, optional `sha256`), `document` (`collection_id`, `document_id`) or `secret` (`name`, a host secret).
- A `url` source is fetched by the platform at create. A private, loopback, link-local or metadata address is refused, on every redirect too, unless the operator allowed private sources. The whole fetch must finish within 60 seconds, and a `sha256` that does not match fails the create. A `user:password@` in the URL is sent as Basic auth and is masked on every read.
- `init_commands` run once, after the files land. A command that fails fails the create.
- The admin-only settings:
  - container `backend.extra_mounts` (host bind mounts);
  - Kubernetes `extra_volumes`, `extra_volume_mounts`, `pod_overrides`, `container_security_context_overrides`, `service_account_name` and `tolerations`;
  - a `gpu.resource_name` other than `nvidia.com/gpu`;
  - any file whose source `kind` is `secret`.

  Setting one, or changing it from the stored value, needs an admin: anyone else gets `forbidden`. A template that already holds one can be updated or deleted only by an admin, whatever the change. `node_selector`, a `gpu` count with the default resource name and `image_pull_policy` are open to every user.
- The Kubernetes overlays (`pod_overrides`, the extra volumes and their mounts) must also stay on a fixed allowlist, for every caller, admins included. A key outside it is a `validation-error` that names it.
- Secrets are masked on read: every `env` value and every `url` password. An update replaces the whole template, so read it, change what you need and send the whole body back: the stored secrets are kept (`env` by key, files by `path`). An admin keeps a URL password for the same scheme, host, port and user; anyone else keeps it only when the whole URL is unchanged. A mask that cannot be put back is a `validation-error`: type the secret again.
- A workspace keeps a full copy of its template. A change or a delete affects only the workspaces created afterwards.
- The built-in template `local-default`, on the built-in `local` provider, ships with the platform. It cannot be changed or deleted (`forbidden`), and nobody can create a template with its id.

## How to
1. Find the provider: `workspaces__list_workspace_provider_choices`, and note its `id` and `kind`.
2. Create the template with a `backend` of that kind, the files, env and start-up commands you need.
3. Build workspaces from it: [create and delete a workspace](workspaces/create-and-delete).
4. To change it: `workspaces__get_workspace_template`, edit the body, then `workspaces__update_workspace_template` with the whole body.
5. Delete it when nobody needs it. The workspaces made from it keep working.

## Tool calls
### List the providers you may use
Answers each provider's id, kind, label and default image, without its configuration.

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_provider_choices", "arguments": {}}
```

Reference: [`workspaces__list_workspace_provider_choices`](reference/tools/workspaces/list_workspace_provider_choices)

### Create a local template
A template on the built-in local provider, with a seeded file, an environment variable and a start-up command.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_template", "arguments": {"entity": {"id": "py-tools", "description": "Python tools on the server", "provider_id": "local", "backend": {"kind": "local"}, "files": [{"path": "README.md", "source": {"kind": "inline", "content": "# Invoice parser\n"}}], "env": {"PYTHONUNBUFFERED": "1"}, "init_commands": ["python3 -m venv .venv"]}}}
```

Reference: [`workspaces__create_workspace_template`](reference/tools/workspaces/create_workspace_template)

### Create a Docker template
A container template sized to two cores and 4 GiB of memory.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_template", "arguments": {"entity": {"id": "py-docker", "description": "Python in a Docker container", "provider_id": "docker-1", "backend": {"kind": "container", "image": "primer/workspace-runtime:1.1", "cpu_cores": 2, "memory_bytes": 4294967296}}}}
```

Reference: [`workspaces__create_workspace_template`](reference/tools/workspaces/create_workspace_template)

### Read a template
Answers the stored template, with its secrets masked.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_template", "arguments": {"id": "py-tools"}}
```

Reference: [`workspaces__get_workspace_template`](reference/tools/workspaces/get_workspace_template)

### Change a template
Replaces the whole template: the `id` argument must equal the body's `id`.

```json
{"toolset_id": "workspaces", "tool_name": "update_workspace_template", "arguments": {"id": "py-tools", "entity": {"id": "py-tools", "description": "Python tools on the server, with pytest", "provider_id": "local", "backend": {"kind": "local"}, "files": [{"path": "README.md", "source": {"kind": "inline", "content": "# Invoice parser\n"}}], "env": {"PYTHONUNBUFFERED": "1"}, "init_commands": ["python3 -m venv .venv", ".venv/bin/pip install pytest"]}}}
```

Reference: [`workspaces__update_workspace_template`](reference/tools/workspaces/update_workspace_template)

### List templates
Pages through every template.

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_templates", "arguments": {"limit": 20}}
```

Reference: [`workspaces__list_workspace_templates`](reference/tools/workspaces/list_workspace_templates)

### Delete a template
Deletes the recipe and answers `{deleted: true, id}`; existing workspaces are untouched.

```json
{"toolset_id": "workspaces", "tool_name": "delete_workspace_template", "arguments": {"id": "py-tools"}}
```

Reference: [`workspaces__delete_workspace_template`](reference/tools/workspaces/delete_workspace_template)

## Gotchas
- `backend.kind` must match the provider's kind. Check it with `workspaces__list_workspace_provider_choices` first.
- An update is a full replace. A field you leave out is gone.
- A template you copied from a read carries masked secrets, and a create refuses a mask (`validation-error`). Type the secrets again in the copy.
- `order_by` on a field that holds a secret (such as `env`) is a `validation-error`.
- The admin-only rule stops a user from rewriting the template, not from building a workspace from it and running code beside its mounts, secret files and service account. An admin must not put those in a template that ordinary users may use.
- `init_commands` are not admin-only. On a local provider they run in a shell on the server.
- A GPU resource name under `kubernetes.io/`, or one that starts with `requests.`, is refused for everyone.
- A CPU or memory of zero or less, and a `pvc_size` that is not a valid quantity, are refused when the template is written. A CPU or memory above 1024 cores or 64 TiB is refused when a workspace is built from it.
- If a Docker workspace's runtime does not answer within 30 seconds, the create fails with `backend-error` and a message that names the image.

## Related
[create and delete a workspace](workspaces/create-and-delete), [workspace providers](workspaces/providers).
