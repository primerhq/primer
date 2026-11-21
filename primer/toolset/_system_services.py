"""Publish a directory from the workspace bound to the calling turn."""

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
from primer.service.bundle import BundleError, validate_bundle
from primer.service.publish import missing_toolset, publish_version, service_named
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err, ok_json
from primer.toolset._system_common import _err_from_validation

if TYPE_CHECKING:
    from primer.api.registries.artifact_storage_registry import ArtifactStorageRegistry
    from primer.api.registries.workspace_registry import WorkspaceRegistry
    from primer.toolset.internal import ToolHandler


class PublishDirectoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Service URL slug.")
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
        when="Use when a prepared bundle in this turn's workspace should serve at /svc/{name}/. A new service defaults to console authentication.",
        args_schema=PublishDirectoryArgs.model_json_schema(),
        examples=[ToolExample(args={"name": "status-app", "path": "apps/status"}, returns="The service and version IDs, version number, and /svc/status-app/ URL.")],
        required_role="admin", requires_workspace=True, interruptible=False,
        preview_args=["name", "path"],
    ), publish
