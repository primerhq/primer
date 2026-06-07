---
slug: api-workspaces
title: REST API - workspaces
summary: Developer reference for workspace providers, templates, workspace instances, file operations, diagnostic exec, and pause/resume.
section: reference
---

Workspace providers, templates, and workspace instances make up the three-tier workspace surface under `/v1`. See the concept and feature pages for background.

```ref:concepts/workspaces
Workspace concept: providers, templates, instances, and the local/container/kubernetes backends.
```

```ref:features/workspaces
Workspace feature guide: creating and managing workspaces from the console.
```

## Endpoints

| Method | Path | What it does |
|---|---|---|
| POST | `/v1/workspace_providers` | Register a workspace backend |
| GET | `/v1/workspace_providers` | List providers |
| GET | `/v1/workspace_providers/{id}` | Fetch one provider |
| DELETE | `/v1/workspace_providers/{id}` | Delete a provider |
| POST | `/v1/workspace_providers/find` | Predicate-based search |
| POST | `/v1/workspace_templates` | Create a template |
| GET | `/v1/workspace_templates` | List templates |
| GET | `/v1/workspace_templates/{id}` | Fetch one template |
| PUT | `/v1/workspace_templates/{id}` | Replace a template |
| DELETE | `/v1/workspace_templates/{id}` | Delete a template |
| POST | `/v1/workspace_templates/find` | Predicate-based search |
| POST | `/v1/workspaces` | Materialise a workspace |
| GET | `/v1/workspaces` | List workspaces |
| GET | `/v1/workspaces/{id}` | Fetch one workspace |
| PATCH | `/v1/workspaces/{id}` | Rename |
| DELETE | `/v1/workspaces/{id}` | Destroy |
| PUT | `/v1/workspaces/{id}/files` | Write a file |
| GET | `/v1/workspaces/{id}/files` | List directory entries |
| DELETE | `/v1/workspaces/{id}/files` | Delete a file or directory |
| GET | `/v1/workspaces/{id}/files/read` | Read a file |
| GET | `/v1/workspaces/{id}/files/info` | Stat a path |
| GET | `/v1/workspaces/{id}/files/download` | Download raw bytes |
| POST | `/v1/workspaces/{id}/files/dir` | Create a directory |
| GET | `/v1/workspaces/{id}/log` | Git commit log |
| POST | `/v1/workspaces/{id}/diagnostic` | Run a diagnostic command |
| POST | `/v1/workspaces/{id}/pause` | Pause (reserved, returns 501) |
| POST | `/v1/workspaces/{id}/resume` | Resume (reserved, returns 501) |

---

## POST /v1/workspace_providers

Register a workspace backend. `PUT` is not defined on this resource; providers are immutable once created.

### Local provider

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/workspace_providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "local-dev",
    "provider": "local",
    "config": {
      "kind": "local",
      "root_path": "~/.primer/workspaces"
    }
  }'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/workspace_providers",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "local-dev",
        "provider": "local",
        "config": {"kind": "local", "root_path": "~/.primer/workspaces"},
    },
)
r.raise_for_status()
--- javascript
await fetch("/v1/workspace_providers", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    id: "local-dev",
    provider: "local",
    config: { kind: "local", root_path: "~/.primer/workspaces" },
  }),
});
```

Response `201`:

```json
{
  "id": "local-dev",
  "provider": "local",
  "config": { "kind": "local", "root_path": "~/.primer/workspaces" }
}
```

### Container provider

Requires Docker accessible via a Unix socket or TCP endpoint. The provider carries connection + reachability only; image and resource limits go in the template.

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/workspace_providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "docker-local",
    "provider": "container",
    "config": {
      "kind": "container",
      "runtime": "docker",
      "connection": { "kind": "socket", "socket_path": "/var/run/docker.sock" },
      "reachability": { "kind": "host_port", "bind_host": "127.0.0.1" }
    }
  }'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/workspace_providers",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "docker-local",
        "provider": "container",
        "config": {
            "kind": "container",
            "runtime": "docker",
            "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
            "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"},
        },
    },
)
r.raise_for_status()
--- javascript
await fetch("/v1/workspace_providers", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    id: "docker-local",
    provider: "container",
    config: {
      kind: "container",
      runtime: "docker",
      connection: { kind: "socket", socket_path: "/var/run/docker.sock" },
      reachability: { kind: "host_port", bind_host: "127.0.0.1" },
    },
  }),
});
```

### Kubernetes provider (in-cluster)

Use `connection.kind = "in_cluster"` when the primer platform itself runs inside the cluster. Use `connection.kind = "kubeconfig"` with a `path` for host-side deployments.

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/workspace_providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "k8s-prod",
    "provider": "kubernetes",
    "config": {
      "kind": "kubernetes",
      "connection": { "kind": "in_cluster" },
      "namespace": "primer-workspaces",
      "reachability": { "kind": "in_cluster" }
    }
  }'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/workspace_providers",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "k8s-prod",
        "provider": "kubernetes",
        "config": {
            "kind": "kubernetes",
            "connection": {"kind": "in_cluster"},
            "namespace": "primer-workspaces",
            "reachability": {"kind": "in_cluster"},
        },
    },
)
r.raise_for_status()
--- javascript
await fetch("/v1/workspace_providers", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    id: "k8s-prod",
    provider: "kubernetes",
    config: {
      kind: "kubernetes",
      connection: { kind: "in_cluster" },
      namespace: "primer-workspaces",
      reachability: { kind: "in_cluster" },
    },
  }),
});
```

---

## POST /v1/workspace_templates

Create a template that describes how to materialise a workspace against a provider.

### Local template

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/workspace_templates \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "local-tpl",
    "description": "Local dev workspace",
    "provider_id": "local-dev",
    "backend": { "kind": "local" }
  }'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/workspace_templates",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "local-tpl",
        "description": "Local dev workspace",
        "provider_id": "local-dev",
        "backend": {"kind": "local"},
    },
)
r.raise_for_status()
--- javascript
await fetch("/v1/workspace_templates", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    id: "local-tpl",
    description: "Local dev workspace",
    provider_id: "local-dev",
    backend: { kind: "local" },
  }),
});
```

### Container template

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/workspace_templates \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "container-tpl",
    "description": "Docker runtime workspace",
    "provider_id": "docker-local",
    "backend": {
      "kind": "container",
      "image": "primer/workspace-runtime:1.0"
    }
  }'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/workspace_templates",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "container-tpl",
        "description": "Docker runtime workspace",
        "provider_id": "docker-local",
        "backend": {"kind": "container", "image": "primer/workspace-runtime:1.0"},
    },
)
r.raise_for_status()
--- javascript
await fetch("/v1/workspace_templates", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    id: "container-tpl",
    description: "Docker runtime workspace",
    provider_id: "docker-local",
    backend: { kind: "container", image: "primer/workspace-runtime:1.0" },
  }),
});
```

### Kubernetes template

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/workspace_templates \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "k8s-tpl",
    "description": "K8s pod workspace",
    "provider_id": "k8s-prod",
    "backend": {
      "kind": "kubernetes",
      "image": "127.0.0.1:30500/primer/workspace-runtime:1.0",
      "entrypoint": ["python", "-m", "primer_runtime.server"],
      "pvc_size": "1Gi"
    }
  }'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/workspace_templates",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "k8s-tpl",
        "description": "K8s pod workspace",
        "provider_id": "k8s-prod",
        "backend": {
            "kind": "kubernetes",
            "image": "127.0.0.1:30500/primer/workspace-runtime:1.0",
            "entrypoint": ["python", "-m", "primer_runtime.server"],
            "pvc_size": "1Gi",
        },
    },
)
r.raise_for_status()
--- javascript
await fetch("/v1/workspace_templates", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    id: "k8s-tpl",
    description: "K8s pod workspace",
    provider_id: "k8s-prod",
    backend: {
      kind: "kubernetes",
      image: "127.0.0.1:30500/primer/workspace-runtime:1.0",
      entrypoint: ["python", "-m", "primer_runtime.server"],
      pvc_size: "1Gi",
    },
  }),
});
```

---

## POST /v1/workspaces

Materialise a workspace from a template. The server assigns an id of the form `ws-<hex>` if `id` is omitted. For container and kubernetes backends, poll `GET /v1/workspaces/{id}` until `phase == "running"`.

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/workspaces \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"template_id": "local-tpl"}'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/workspaces",
    headers={"Authorization": f"Bearer {token}"},
    json={"template_id": "local-tpl"},
)
r.raise_for_status()
ws = r.json()
--- javascript
const r = await fetch("/v1/workspaces", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ template_id: "local-tpl" }),
});
const ws = await r.json();
```

Response `201`:

```json
{
  "id": "ws-a1b2c3d4",
  "name": null,
  "template_id": "local-tpl",
  "phase": "running",
  "created_at": "2026-06-08T12:00:00Z",
  "failure_reason": null,
  "last_probe_ok": false,
  "runtime_meta": null
}
```

`phase` values: `running`, `failed`. Container and kubernetes workspaces may briefly show intermediate states while booting.

---

## PATCH /v1/workspaces/{id}

Rename a workspace. The `id` handle is immutable; `name` is the display label.

```code-tabs:curl,python,javascript
--- curl
curl -s -X PATCH https://primer.example/v1/workspaces/ws-a1b2c3d4 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "My workspace"}'
--- python
import httpx
r = httpx.patch(
    "https://primer.example/v1/workspaces/ws-a1b2c3d4",
    headers={"Authorization": f"Bearer {token}"},
    json={"name": "My workspace"},
)
r.raise_for_status()
--- javascript
await fetch("/v1/workspaces/ws-a1b2c3d4", {
  method: "PATCH",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ name: "My workspace" }),
});
```

---

## PUT /v1/workspaces/{id}/files

Write a file. Pass `path` as a query parameter. Parents are created automatically. Writing to `.state/` or `.tmp/` is rejected with `400`.

```code-tabs:curl,python,javascript
--- curl
curl -s -X PUT "https://primer.example/v1/workspaces/ws-a1b2c3d4/files?path=notes.txt" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"content": "hello world", "encoding": "text"}'
--- python
import httpx
r = httpx.put(
    "https://primer.example/v1/workspaces/ws-a1b2c3d4/files",
    params={"path": "notes.txt"},
    headers={"Authorization": f"Bearer {token}"},
    json={"content": "hello world", "encoding": "text"},
)
r.raise_for_status()  # 204 No Content
--- javascript
await fetch("/v1/workspaces/ws-a1b2c3d4/files?path=notes.txt", {
  method: "PUT",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ content: "hello world", encoding: "text" }),
});
```

Returns `204` on success. Use `encoding: "base64"` for binary content.

---

## GET /v1/workspaces/{id}/files/read

Read a file. Returns `FileReadResponse`.

```code-tabs:curl,python,javascript
--- curl
curl -s "https://primer.example/v1/workspaces/ws-a1b2c3d4/files/read?path=notes.txt&encoding=text" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/workspaces/ws-a1b2c3d4/files/read",
    params={"path": "notes.txt", "encoding": "text"},
    headers={"Authorization": f"Bearer {token}"},
)
data = r.json()
--- javascript
const r = await fetch(
  "/v1/workspaces/ws-a1b2c3d4/files/read?path=notes.txt&encoding=text",
  { headers: { "Authorization": `Bearer ${token}` } },
);
const data = await r.json();
```

Response `200`:

```json
{
  "path": "notes.txt",
  "encoding": "text",
  "content": "hello world",
  "size_bytes": 11
}
```

---

## GET /v1/workspaces/{id}/files

List a directory. Returns an object with an `items` array of `FileEntry` objects.

```code-tabs:curl,python,javascript
--- curl
curl -s "https://primer.example/v1/workspaces/ws-a1b2c3d4/files?path=." \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/workspaces/ws-a1b2c3d4/files",
    params={"path": "."},
    headers={"Authorization": f"Bearer {token}"},
)
listing = r.json()
--- javascript
const r = await fetch("/v1/workspaces/ws-a1b2c3d4/files?path=.", {
  headers: { "Authorization": `Bearer ${token}` },
});
const listing = await r.json();
```

Each `FileEntry`:

```json
{
  "path": "notes.txt",
  "kind": "file",
  "size_bytes": 11,
  "modified_at": "2026-06-08T12:01:00Z"
}
```

`kind` is one of `file`, `dir`, or `symlink`.

---

## DELETE /v1/workspaces/{id}/files

Delete a file or directory. Add `?recursive=true` to delete a non-empty directory; without it a non-empty directory returns `400`.

```code-tabs:curl,python,javascript
--- curl
curl -s -X DELETE "https://primer.example/v1/workspaces/ws-a1b2c3d4/files?path=old-dir&recursive=true" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.delete(
    "https://primer.example/v1/workspaces/ws-a1b2c3d4/files",
    params={"path": "old-dir", "recursive": "true"},
    headers={"Authorization": f"Bearer {token}"},
)
r.raise_for_status()  # 204
--- javascript
await fetch("/v1/workspaces/ws-a1b2c3d4/files?path=old-dir&recursive=true", {
  method: "DELETE",
  headers: { "Authorization": `Bearer ${token}` },
});
```

---

## POST /v1/workspaces/{id}/files/dir

Create a directory. Parent directories are created automatically. Returns `400` if the path already exists.

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST "https://primer.example/v1/workspaces/ws-a1b2c3d4/files/dir?path=src/utils" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/workspaces/ws-a1b2c3d4/files/dir",
    params={"path": "src/utils"},
    headers={"Authorization": f"Bearer {token}"},
)
r.raise_for_status()  # 204
--- javascript
await fetch("/v1/workspaces/ws-a1b2c3d4/files/dir?path=src/utils", {
  method: "POST",
  headers: { "Authorization": `Bearer ${token}` },
});
```

---

## POST /v1/workspaces/{id}/diagnostic

Run a diagnostic shell command inside the workspace. The command must start with one of the allowed names: `echo`, `pwd`, `whoami`, `uname`, `ls`. Anything else returns `400`. `timeout_seconds` is capped at 30.

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/workspaces/ws-a1b2c3d4/diagnostic \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command": "echo hi", "timeout_seconds": 10}'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/workspaces/ws-a1b2c3d4/diagnostic",
    headers={"Authorization": f"Bearer {token}"},
    json={"command": "echo hi", "timeout_seconds": 10},
)
result = r.json()
--- javascript
const r = await fetch("/v1/workspaces/ws-a1b2c3d4/diagnostic", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ command: "echo hi", timeout_seconds: 10 }),
});
const result = await r.json();
```

Response `200`:

```json
{
  "exit_code": 0,
  "stdout": "hi\n",
  "stderr": ""
}
```

---

## GET /v1/workspaces/{id}/log

Return the git commit log for the workspace's `.state` repo. Pass `?limit=N` to cap results.

```code-tabs:curl,python,javascript
--- curl
curl -s "https://primer.example/v1/workspaces/ws-a1b2c3d4/log?limit=20" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/workspaces/ws-a1b2c3d4/log",
    params={"limit": 20},
    headers={"Authorization": f"Bearer {token}"},
)
log = r.json()
--- javascript
const r = await fetch("/v1/workspaces/ws-a1b2c3d4/log?limit=20", {
  headers: { "Authorization": `Bearer ${token}` },
});
const log = await r.json();
```

---

## POST /v1/workspaces/{id}/pause and /resume

Workspace-level pause/resume is reserved in v1. Both routes return `501` with `{"detail": {"error": "not_implemented"}}`. Session-level pause/resume is available under `/v1/workspaces/{id}/sessions/{session_id}/pause`.

---

## Error responses

All error responses follow RFC 7807:

```json
{
  "type": "/errors/not-found",
  "title": "Not Found",
  "status": 404,
  "detail": "workspace ws-missing not found",
  "instance": "/v1/workspaces/ws-missing"
}
```

Common status codes: `400` invalid path or argument, `404` resource not found, `422` validation error, `501` not implemented.
