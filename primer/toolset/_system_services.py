"""Publish a directory from the workspace bound to the calling turn.

The publish is bound to the content the approver was shown: ``publish_service``
requires the ``bundle_digest`` of the directory as captured by
``inspect_service_bundle``, and a capture that no longer matches it is refused
rather than promoted (review 2026-11-23, 5.8).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from primer.access.person import person_for_tool
from primer.access.workspaces import require_workspace
from primer.model.access import AccessLevel
from primer.int.storage_provider import StorageProvider
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.except_ import AccessDeniedError, BadRequestError, ConflictError, NotFoundError, ProviderError
from primer.model.service import Service
from primer.model.yield_ import ToolContext
from primer.service.bundle import BundleError, bundle_digest, validate_bundle
from primer.service.publish import missing_toolset, publish_version, service_named
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err, ok_json
from primer.toolset._system_common import _err_from_validation

if TYPE_CHECKING:
    from primer.api.registries.artifact_storage_registry import ArtifactStorageRegistry
    from primer.api.registries.workspace_registry import WorkspaceRegistry
    from primer.toolset.internal import ToolHandler


_DIGEST_RE = r"^[0-9a-f]{64}$"


class PublishDirectoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Service URL slug.")
    path: str = Field(min_length=1, max_length=4096, description="Dedicated bundle directory relative to this turn's workspace.")
    bundle_digest: str = Field(
        min_length=64, max_length=64, pattern=_DIGEST_RE,
        description="The digest inspect_service_bundle returned for this path; the approval is bound to it and a capture that no longer matches is refused.",
    )


class InspectServiceBundleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096, description="Dedicated bundle directory relative to this turn's workspace.")


def publish_service_tool(
    toolset_id: str, storage_provider: StorageProvider,
    workspace_registry: WorkspaceRegistry | None, artifact_storage_registry: ArtifactStorageRegistry | None,
) -> tuple[Tool, ToolHandler]:
    async def publish(arguments: dict[str, Any], *, ctx: ToolContext | None = None) -> ToolCallResult:
        if ctx is None or not ctx.session_id or not ctx.workspace_id:
            return err("publish_service requires a calling workspace", error_type="bad-request")
        person = person_for_tool(ctx)
        if not person.is_admin:
            return err("service publishing requires an administrator", error_type="forbidden")
        try:
            args = PublishDirectoryArgs.model_validate(arguments)
            new = Service(name=args.name, description="")
        except ValidationError as exc:
            return _err_from_validation(exc)
        if workspace_registry is None or artifact_storage_registry is None:
            return err("workspace publishing is unavailable in this process", error_type="unavailable")
        try:
            await require_workspace(storage_provider, person, ctx.workspace_id, AccessLevel.READ)
            selected = await service_named(storage_provider, args.name)
            if selected is not None and selected.harness_id is not None:
                raise ConflictError("publish a harness-managed service through its harness")
            workspace = await workspace_registry.get_workspace(ctx.workspace_id)
            data = await workspace.capture_service_bundle(args.path)
            bundle = await asyncio.to_thread(validate_bundle, data, workspace_capture=True)
            if bundle_digest(bundle) != args.bundle_digest:
                # The approval card bound the digest the approver was shown; a
                # capture that no longer matches it means the approved content
                # is not what is on disk.
                raise ConflictError(
                    "the bundle content changed after it was inspected: run inspect_service_bundle "
                    "again and approve the new digest before publishing"
                )
            unknown = await missing_toolset(storage_provider, bundle)
            if unknown is not None:
                raise BundleError(f"manifest grants unknown toolset {unknown!r}")
            artifacts = await artifact_storage_registry.get_default()
            if selected is not None:
                current = await storage_provider.get_storage(Service).get(selected.id)
                if current is None or current.name != args.name:
                    raise ConflictError("service was deleted or renamed during capture; try again")
                selected = current
            else:
                try:
                    selected = await storage_provider.get_storage(Service).create(new)
                except ConflictError:
                    selected = await service_named(storage_provider, args.name)
                    if selected is None:
                        raise
            if selected.harness_id is not None:
                raise ConflictError("publish a harness-managed service through its harness")
            version = await publish_version(storage_provider, artifacts, service=selected, bundle=bundle)
        except AccessDeniedError as exc:
            return err(str(exc), error_type="forbidden")
        except NotFoundError as exc:
            return err(str(exc), error_type="not-found")
        except ConflictError as exc:
            return err(str(exc), error_type="conflict")
        except (BadRequestError, BundleError) as exc:
            return err(str(exc), error_type="bad-request")
        except ProviderError as exc:
            return err(str(exc), error_type="provider-error")
        return ok_json({"service_id": selected.id, "version_id": version.id, "version": version.version, "url": f"/svc/{selected.name}/"})

    return make_tool(
        id="publish_service", toolset_id=toolset_id,
        purpose="Publish and activate a service version from a workspace directory.",
        when=(
            "Use when a prepared bundle in this turn's workspace should serve at /svc/{name}/. "
            "Call inspect_service_bundle on the path first and pass its digest as bundle_digest: "
            "the approval is bound to that digest, and a publish whose capture no longer matches it "
            "is refused. A new service defaults to console authentication."
        ),
        args_schema=PublishDirectoryArgs.model_json_schema(),
        examples=[ToolExample(
            args={"name": "status-app", "path": "apps/status",
                  "bundle_digest": "3f7e2a9c4d1b806552cf1a9e0b4c7d8f3165a0e9d2c4b8f7a0531e6d9c2b4f70"},
            returns="The service and version IDs, version number, and /svc/status-app/ URL."),
        ],
        required_role="admin", requires_workspace=True, interruptible=False,
        preview_args=["name", "path", "bundle_digest"],
    ), publish


def inspect_service_bundle_tool(
    toolset_id: str, storage_provider: StorageProvider,
    workspace_registry: WorkspaceRegistry | None,
) -> tuple[Tool, ToolHandler]:
    async def inspect(arguments: dict[str, Any], *, ctx: ToolContext | None = None) -> ToolCallResult:
        if ctx is None or not ctx.session_id or not ctx.workspace_id:
            return err("inspect_service_bundle requires a calling workspace", error_type="bad-request")
        person = person_for_tool(ctx)
        if not person.is_admin:
            return err("service publishing requires an administrator", error_type="forbidden")
        try:
            args = InspectServiceBundleArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if workspace_registry is None:
            return err("workspace publishing is unavailable in this process", error_type="unavailable")
        try:
            await require_workspace(storage_provider, person, ctx.workspace_id, AccessLevel.READ)
            workspace = await workspace_registry.get_workspace(ctx.workspace_id)
            data = await workspace.capture_service_bundle(args.path)
            bundle = await asyncio.to_thread(validate_bundle, data, workspace_capture=True)
        except AccessDeniedError as exc:
            return err(str(exc), error_type="forbidden")
        except NotFoundError as exc:
            return err(str(exc), error_type="not-found")
        except (BadRequestError, BundleError) as exc:
            return err(str(exc), error_type="bad-request")
        except ProviderError as exc:
            return err(str(exc), error_type="provider-error")
        # The digest and the file list are what the approver is shown; publish_service
        # refuses a capture whose digest no longer matches.
        return ok_json({"bundle_digest": bundle_digest(bundle), "files": sorted(bundle.files)})

    return make_tool(
        id="inspect_service_bundle", toolset_id=toolset_id,
        purpose="Show the digest and file list of a workspace bundle directory, for approving a publish.",
        when=(
            "Use when you want to show the approver exactly what a publish will deploy before "
            "calling publish_service: it captures and validates the directory and returns the "
            "bundle_digest to pass to publish_service and the file list to show the approver. "
            "It changes nothing."
        ),
        args_schema=InspectServiceBundleArgs.model_json_schema(),
        examples=[ToolExample(args={"path": "apps/status"}, returns="The bundle digest and the bundle's file list.")],
        required_role="admin", requires_workspace=True, read_only=True,
    ), inspect
