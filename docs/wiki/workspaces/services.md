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

- Service management and publishing have generated route-backed tools. Workspace sessions also offer `system__publish_service` and, before it, `system__inspect_service_bundle`. The browser `Primer.tool` API remains planned.
- Publishing runs the workspace's code on the API host: the bundle's `functions.py` executes there (in the same hardened python-runner sandbox as python tools, not in the workspace's container), and the app's JS is served first-party on the console origin. The admin approving a publish approves that.
- A publish is bound to its content: `system__publish_service` requires the `bundle_digest` that `system__inspect_service_bundle` returned for the path, the approval card shows the digest, and a capture that no longer matches it is refused until it is re-inspected and re-approved.
- Directory capture can take up to 30 seconds. If a prior capture is still reading the workspace, a new attempt returns a conflict. Capture failures occur before a service or version is created.
- Read the exact publish schema before supplying the manifest and file payloads. A successful publish creates a new immutable version; activating an earlier version is a rollback, not a rewrite of historical artifact bytes.
- Each artifact retains its storage-provider identity. Changing the active artifact store routes new writes there; reads of existing DB or filesystem versions still use their original provider.
- Service assets and bundle functions use their native routes. A binary asset or function response must be consumed in its actual format; listing the service does not exercise either path.

## How to

From an admin workspace session, prepare a dedicated directory with your app files
and optional `service.yaml`. Call `system__inspect_service_bundle` with
`{"path":"apps/status"}`: it validates the directory and returns the
`bundle_digest` and the file list to show the approver. Then call
`system__publish_service` with
`{"name":"status-app","path":"apps/status","bundle_digest":"..."}`. The default
policy requires an admin to approve the card (name, path and digest), including
after an upgrade. If the directory changes after approval, the publish is
refused: inspect again and approve the new digest. Existing operator-customized
policies remain in effect, including disabled policies. On an uncached lookup,
a malformed matching publisher policy refuses the call rather than being treated
as absent. A cached decision can still be served while a background refresh runs;
a failed refresh retains that decision until a successful refresh or invalidation.
Approve the publish when prompted.
The result gives the active version number, version ID and URL (`/svc/status-app/`).
The first call creates a console-authenticated service. Later calls publish a new
version and keep its current viewer authentication. The directory must contain
ordinary files, within 200 files and 10 MiB total. Links and workspace state
directories are refused. Container and Kubernetes workspaces require runtime 1.7.

To publish an already prepared archive, including from an external MCP client:

1. An admin creates the service and reads its saved identity and name.
2. Build a gzipped tar archive containing the app files (and optional `service.yaml`). Call `services__publish_service_version` with `service_id` and `body`: the base64 encoding of those archive bytes, not a file path or a JSON manifest. `activate` defaults to true; use false to stage a version. List versions and verify the published manifest and file references.
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
| [`system__inspect_service_bundle`](reference/tools/system/inspect_service_bundle) | Show a workspace bundle's digest and file list, before approving a publish. | admin |
| [`system__publish_service`](reference/tools/system/publish_service) | Publish and activate a directory from the calling workspace. | admin |
| [`svc__svc_gateway_function`](reference/tools/svc/svc_gateway_function) | Call a bundle function in the python-runner sandbox. | user |
| [`svc__svc_serve`](reference/tools/svc/svc_serve) | Serve a service asset. | user |

## Related

[Artifact storage](providers/artifact-storage), [configure artifact storage](runbooks/configure-artifact-storage), [workspace files and log](workspaces/files-and-log).
