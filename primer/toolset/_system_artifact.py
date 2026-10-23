"""System tools for probing and selecting artifact-storage providers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from primer.artifact.probe import probe_artifact_provider
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

    tool = make_tool(
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
    return {tool.id: (tool, _test)}


__all__ = ["artifact_store_tools"]
