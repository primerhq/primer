---
title: "Services and published versions"
summary: "Publish service bundles, activate versions, and understand immutable artifacts and service access."
audience: admin
order: 10
related: [providers/artifact-storage, runbooks/configure-artifact-storage, workspaces/files-and-log]
---

## What it is

A service is an admin-managed name and a sequence of published bundle versions. Publishing stores the bundle's actual files as artifacts; activating a version selects what the service serves. A metadata row or an empty version is not evidence that an application was published.

## Concepts

- Service management and publishing have generated route-backed tools. There is no separate curated `publish_service` tool, and the planned browser `Primer.tool` API is not an implemented capability.
- Read the exact publish schema before supplying the manifest and file payloads. A successful publish creates a new immutable version; activating an earlier version is a rollback, not a rewrite of historical artifact bytes.
- Each artifact retains its storage-provider identity. Changing the active artifact store routes new writes there; reads of existing DB or filesystem versions still use their original provider.
- Service assets and bundle functions use their native routes. A binary asset or function response must be consumed in its actual format; listing the service does not exercise either path.

## How to

1. An admin creates the service and reads its saved identity and name.
2. Inspect the publish operation and supply a real bundle. List versions and verify the published manifest and file references.
3. Activate the desired version, read its served bytes and verify a representative function if the bundle defines one.
4. Roll back by activating an existing version. Delete the service only when intended: deletion cascades to its versions and artifacts.

## Tool calls

`svc__svc_gateway_function` calls a published bundle function with
`{"name": "my-app", "fn": "double", "body": {"n": 21}}`. The `body` object is
the function's keyword arguments, checked against its published schema. The
service's viewer authentication and the tool's effective approval policy apply.


The operation table below distinguishes service management from native asset and function routes. Inspect `system__describe_tool` for the arguments and effective gate before a write. An external MCP call cannot park for a required approval.

## Gotchas

- Deleting a service removes its versions and artifacts. A server's service-name resolution cache can briefly retain the old name after deletion.
- An artifact provider cannot be removed or relocated while it holds artifacts, and the active provider cannot be deleted. Use the [artifact-storage runbook](runbooks/configure-artifact-storage) to verify restoration and deletion safely.
- Reserved future manifest fields do not make planned dispatch APIs available.

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`services__activate_service_version`](reference/tools/services/activate_service_version) | Activate (or roll back to) a published version. | admin |
| [`services__create`](reference/tools/services/create) | Create Service. | admin |
| [`services__delete`](reference/tools/services/delete) | Delete Service. | admin |
| [`services__find`](reference/tools/services/find) | Find Service with predicate. | admin |
| [`services__get`](reference/tools/services/get) | Get Service by id. | admin |
| [`services__list`](reference/tools/services/list) | List Service. | admin |
| [`services__list_service_versions`](reference/tools/services/list_service_versions) | List a service's published versions, newest first. | admin |
| [`services__publish_service_version`](reference/tools/services/publish_service_version) | Publish a bundle as a new service version. | admin |
| [`services__update`](reference/tools/services/update) | Replace Service. | admin |
| [`svc__svc_gateway_function`](reference/tools/svc/svc_gateway_function) | Call a bundle function in the python-runner sandbox. | user |
| [`svc__svc_serve`](reference/tools/svc/svc_serve) | Serve a service asset. | user |

## Related

[Artifact storage](providers/artifact-storage), [configure artifact storage](runbooks/configure-artifact-storage), [workspace files and log](workspaces/files-and-log).
