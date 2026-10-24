"""System tools for probing and selecting artifact-storage providers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from primer.artifact.active import read_active_artifact_store, set_active_artifact_store
from primer.artifact.probe import probe_artifact_provider
from primer.common.entity_checks import EntityCheckError
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.provider import ArtifactStorageProvider
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err, ok
from primer.toolset._system_common import SYSTEM_TOOLSET_ID, _err_from_validation
from primer.toolset.internal import ToolHandler


if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


class _ArtifactProviderIdArgs(BaseModel):
    id: str = Field(..., min_length=1, description="Artifact-storage provider id.")


class _GetActiveArtifactStoreArgs(BaseModel):
    """No arguments."""


class _SetActiveArtifactStoreArgs(BaseModel):
    provider_id: str = Field(..., min_length=1, description="Artifact-storage provider id to select for new writes.")


def artifact_store_tools(
    *, storage_provider: "StorageProvider",
) -> dict[str, tuple[Tool, ToolHandler]]:
    """Build the artifact-storage operations for the system toolset."""
    providers = storage_provider.get_storage(ArtifactStorageProvider)

    async def _test(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _ArtifactProviderIdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        row = await providers.get(args.id)
        if row is None:
            return err(
                f"ArtifactStorageProvider {args.id!r} does not exist",
                error_type="not-found",
            )
        result = await probe_artifact_provider(
            row,
            storage_provider=storage_provider,
        )
        return ok(result.as_dict())

    test_tool = make_tool(
        id="test_artifact_storage_provider",
        toolset_id=SYSTEM_TOOLSET_ID,
        purpose="Test a saved artifact-storage provider with a live round trip.",
        when=(
            "Use when validating a saved provider before selecting it. "
            "Returns ``{ok: true}``, a failing ``step`` and redacted ``error``, "
            "or ``type=not-found`` for an unknown id."
        ),
        args_schema=_ArtifactProviderIdArgs.model_json_schema(),
        examples=[
            ToolExample(
                args={"id": "artifact-storage-1"},
                returns="``{ok: true}`` or a failing step and redacted error",
            ),
        ],
        required_role="admin",
        interruptible=False,
        preview_args=("id",),
    )
    async def _get_active(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            _GetActiveArtifactStoreArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        row = await read_active_artifact_store(storage_provider)
        provider = await providers.get(row.provider_id)
        return ok({
            "provider_id": row.provider_id,
            "provider": provider.provider.value if provider is not None else None,
        })

    async def _set_active(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _SetActiveArtifactStoreArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            row = await set_active_artifact_store(storage_provider, args.provider_id)
        except EntityCheckError as exc:
            return err(exc.tool_message(), error_type=exc.tool_error_type)
        provider = await providers.get(row.provider_id)
        return ok({
            "provider_id": row.provider_id,
            "provider": provider.provider.value if provider is not None else None,
        })

    get_tool = make_tool(
        id="get_active_artifact_store",
        toolset_id=SYSTEM_TOOLSET_ID,
        purpose="Read the artifact store selected for new writes.",
        when=(
            "Use when reading which artifact store is selected for new writes, "
            "such as before changing providers. Returns the selected provider id and backend kind."
        ),
        args_schema=_GetActiveArtifactStoreArgs.model_json_schema(),
        examples=[ToolExample(args={}, returns='``{provider_id: "artifact-storage-default", provider: "db"}``')],
        required_role="admin",
    )
    set_tool = make_tool(
        id="set_active_artifact_store",
        toolset_id=SYSTEM_TOOLSET_ID,
        purpose="Probe an artifact-storage provider and select it for new writes.",
        when=(
            "Use when switching where new artifact bytes are written, after testing or registering the provider. "
            "An unknown or unusable provider returns ``type=validation-error`` and the current selection is unchanged. "
            "A provider deleted while it was being checked, or a selection someone else made meanwhile, returns ``type=conflict`` "
            "and the current selection is unchanged. "
            "Existing artifacts remain where they were written."
        ),
        args_schema=_SetActiveArtifactStoreArgs.model_json_schema(),
        examples=[ToolExample(args={"provider_id": "artifact-storage-1"}, returns="the selected provider id and backend kind")],
        required_role="admin",
        interruptible=False,
        preview_args=("provider_id",),
    )
    return {
        test_tool.id: (test_tool, _test),
        get_tool.id: (get_tool, _get_active),
        set_tool.id: (set_tool, _set_active),
    }


__all__ = ["artifact_store_tools"]
