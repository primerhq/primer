---
title: "Workspace providers"
summary: "Admin: register the compute backends (local, container, Kubernetes) that workspaces are built on."
audience: admin
order: 5
tools: [workspaces__list_workspace_providers, workspaces__get_workspace_provider, workspaces__create_workspace_provider, workspaces__delete_workspace_provider]
related: [workspaces/templates, workspaces/what-a-workspace-is]
---
## What it is
A workspace provider (`WorkspaceProvider`) is a registered compute backend that workspaces are built on. Templates point at it by `provider_id`. Every tool on this page needs an admin, because a provider row holds its connection.

## Concepts
- `provider` is `local`, `container` or `kubernetes`, and `config.kind` must match it.
- A `local` config has one field, `root_path` (`~/.primer/workspaces` by default): each workspace gets its own folder under it, on the server's disk.
- A `container` config has:
  - `runtime`: `docker`, `podman` or `containerd`;
  - `connection`: `{"kind": "socket", "socket_path": ...}`, or `{"kind": "remote", "url": ...}` with optional mTLS `tls_ca`, `tls_cert` and `tls_key`;
  - `reachability`, how the platform reaches the runtime inside each container: `{"kind": "host_port", "bind_host": "127.0.0.1"}` or `{"kind": "bridge_network", "network_name": ...}`;
  - `image_pull_secrets`, optional.
- A `kubernetes` config has:
  - `connection`: `in_cluster`, `kubeconfig` (`path`, optional `context`) or `service_account_token` (`apiserver_url`, `ca_data`, `token`);
  - `namespace`, where the workspaces are created;
  - `reachability`: `in_cluster`, `ingress` (a `url_template`) or `gateway_httproute`;
  - `variant`: `system` (the default). `agent_sandbox` is reserved and not implemented: leave `variant` at `system`;
  - `image_pull_secrets`, optional.
- Secrets in a config (`tls_key`, a service account `token`) come back masked on every read.
- The built-in `local` provider ships with the platform, and the built-in `local-default` template uses it. Nobody can create a provider with its id, and nobody can delete it.
- There is no update tool. To change a provider, delete it and create it again.
- A delete is refused with `conflict` while a template or a workspace still points at the provider. A delete also drops the platform's cached connection to that backend.
- A user never reads a provider. A user sees providers through `workspaces__list_workspace_provider_choices` (id, kind, label and default image, nothing more) and picks one through a template: [workspace templates](workspaces/templates).
- A local provider shares one process's file locks. In a deployment whose API and workers run in separate processes, the operator can make the platform refuse local workspaces (`local_workspaces.refuse_when_distributed`); a refused workspace is kept on disk, not lost.

## How to
1. Decide the backend: local for one server, container for Docker, Podman or containerd, Kubernetes for a cluster.
2. Create the provider with a config of that kind.
3. Create a template on it, then a workspace from the template, to check that it works: [workspace templates](workspaces/templates).
4. To change the connection, move its templates and workspaces off it, delete it, and create it again.

## Tool calls
### Create a Docker provider
Registers the local Docker daemon, reached through its socket, with workspace ports bound to the loopback interface. Needs an admin.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_provider", "arguments": {"entity": {"id": "docker-1", "provider": "container", "config": {"kind": "container", "runtime": "docker", "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"}, "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"}}}}}
```

Reference: [`workspaces__create_workspace_provider`](reference/tools/workspaces/create_workspace_provider)

### Create a Kubernetes provider
Registers the cluster the platform runs in. Needs an admin.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_provider", "arguments": {"entity": {"id": "k3s", "provider": "kubernetes", "config": {"kind": "kubernetes", "connection": {"kind": "in_cluster"}, "namespace": "primer-workspaces", "reachability": {"kind": "in_cluster"}}}}}
```

Reference: [`workspaces__create_workspace_provider`](reference/tools/workspaces/create_workspace_provider)

### List providers
Pages through the providers with their full configuration, secrets masked. Needs an admin.

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_providers", "arguments": {"limit": 50, "order_by": ["id:asc"]}}
```

Reference: [`workspaces__list_workspace_providers`](reference/tools/workspaces/list_workspace_providers)

### Get a provider
Reads one provider's configuration. Needs an admin.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_provider", "arguments": {"id": "docker-1"}}
```

Reference: [`workspaces__get_workspace_provider`](reference/tools/workspaces/get_workspace_provider)

### Delete a provider
Deletes the provider once nothing points at it. Needs an admin.

```json
{"toolset_id": "workspaces", "tool_name": "delete_workspace_provider", "arguments": {"id": "docker-1"}}
```

Reference: [`workspaces__delete_workspace_provider`](reference/tools/workspaces/delete_workspace_provider)

## Gotchas
- Every tool on this page needs an admin. A run below that floor gets `forbidden`.
- A config shaped for another kind is a `validation-error`: `provider` and `config.kind` must agree.
- A delete refused with `conflict` names the first template or workspace that still points at the provider. Delete or move those first.
- A provider row says nothing about its health. Create a workspace on it to find out; a backend that does not answer fails the create with `backend-error`.

## Related
[workspace templates](workspaces/templates), [what a workspace is](workspaces/what-a-workspace-is).
