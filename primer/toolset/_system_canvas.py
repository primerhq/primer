"""The canvas settings, as system tools (pivot P5, Task 15; plan Decision 14).

The tool twins of ``GET`` and ``PUT /v1/canvas_settings``, admin-only like the routes: ``get_canvas_settings`` reads the one settings
row (or the defaults), ``update_canvas_settings`` changes the fields it is given and keeps the others. They live in the ``system``
toolset, not in ``canvas``, which keeps the design's three tools.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from primer.canvas.settings import read_canvas_settings, write_canvas_settings
from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.toolset._describe import make_tool
from primer.toolset._helpers import ok
from primer.toolset._system_common import SYSTEM_TOOLSET_ID, _err_from_validation
from primer.toolset.internal import ToolHandler

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


class _GetCanvasSettingsArgs(BaseModel):
    """No arguments."""

    model_config = ConfigDict(extra="forbid")


def _partial_settings_args() -> type[BaseModel]:
    """Every setting of :class:`CanvasSettings` (not the id), each optional, with the same bounds and descriptions: the arguments of
    ``update_canvas_settings``. Built from the model, so a setting added there is a tool argument too. An unknown name is refused."""
    fields: dict[str, Any] = {}
    for name, info in CanvasSettings.model_fields.items():
        if name == "id":
            continue
        partial = copy.copy(info)
        partial.default = None
        partial.default_factory = None
        fields[name] = (info.annotation | None, partial)
    return create_model(  # type: ignore[call-overload]
        "_UpdateCanvasSettingsArgs", __config__=ConfigDict(extra="forbid"), **fields,
    )


_UpdateCanvasSettingsArgs = _partial_settings_args()


def canvas_settings_tools(storage_provider: "StorageProvider") -> dict[str, tuple[Tool, ToolHandler]]:
    """``get_canvas_settings`` and ``update_canvas_settings``, both admin-only and neither pausing nor needing a workspace."""

    async def _get(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            _GetCanvasSettingsArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        return ok((await read_canvas_settings(storage_provider)).model_dump(mode="json"))

    async def _update(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            given = _UpdateCanvasSettingsArgs.model_validate(arguments).model_dump(exclude_unset=True)
            current = (await read_canvas_settings(storage_provider)).model_dump()
            settings = CanvasSettings.model_validate({**current, **given, "id": CANVAS_SETTINGS_ID})
        except ValidationError as exc:
            return _err_from_validation(exc)
        stored = await write_canvas_settings(storage_provider, settings)
        return ok(stored.model_dump(mode="json"))

    get_tool = make_tool(
        id="get_canvas_settings",
        toolset_id=SYSTEM_TOOLSET_ID,
        purpose="Read the canvas settings: media policy, strict mode and limits.",
        when=(
            "Use when checking where session canvases may load images and audio from, whether an Html element waits for the user's "
            "Run, whether agents have the canvas tools, or the canvas limits. Returns the stored settings, or the defaults when none "
            "were saved."
        ),
        args_schema=_GetCanvasSettingsArgs.model_json_schema(),
        examples=[ToolExample(args={}, returns='``{media_policy: "any-https", strict_mode: false, max_elements: 200, ...}``')],
        required_role="admin",
    )
    update_tool = make_tool(
        id="update_canvas_settings",
        toolset_id=SYSTEM_TOOLSET_ID,
        purpose="Change the canvas settings; fields you leave out keep their values.",
        when=(
            "Use when an operator asks to change where canvases load media from (``media_policy``: ``any-https``, ``allowlist`` "
            "with ``media_allowlist`` hosts, or ``none``), strict mode, whether agents have the canvas tools, or a canvas limit. "
            "Returns the stored settings. A value out of range, an unknown setting or a host that is not one returns "
            "``type=validation-error`` and nothing changes."
        ),
        args_schema=_UpdateCanvasSettingsArgs.model_json_schema(),
        examples=[
            ToolExample(
                args={"media_policy": "allowlist", "media_allowlist": ["media.example.com", "*.cdn.example.com"]},
                returns="the stored settings",
            ),
            ToolExample(args={"strict_mode": True}, returns="the stored settings, every other field as it was"),
        ],
        required_role="admin",
        preview_args=tuple(name for name in CanvasSettings.model_fields if name != "id"),
    )
    return {get_tool.id: (get_tool, _get), update_tool.id: (update_tool, _update)}


__all__ = ["canvas_settings_tools"]
