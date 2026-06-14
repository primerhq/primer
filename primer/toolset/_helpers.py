"""Shared result helpers for the internal toolset handlers.

Every internal toolset turns a Python payload (or an error) into a
:class:`primer.model.chat.ToolCallResult` the agent loop can hand back to
the model. Two shapes recur across every toolset module:

* :func:`err` -- a uniform error envelope ``{"type", "message"}`` with
  ``is_error=True``. Byte-identical in every toolset, so it lives here.
* an ``ok`` family that JSON-encodes a success payload with
  ``is_error=False``. Two variants exist because some toolsets emit only
  plain JSON values while others emit Pydantic models / lists of models:

  - :func:`ok_json` -- ``json.dumps(payload, default=str)``. For toolsets
    whose payloads are already plain JSON-compatible values.
  - :func:`to_json` / :func:`ok` -- model-aware: a :class:`BaseModel`
    serialises via ``model_dump_json()``, a list serialises each element
    (models via ``model_dump(mode="json")``), everything else falls back
    to ``json.dumps(..., default=str)``.

Both ``ok`` variants are preserved exactly as they were inlined per
module so the wire output of every tool is unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from primer.model.chat import ToolCallResult


def err(message: str, *, error_type: str = "tool-error") -> ToolCallResult:
    """Build the uniform ``{"type", "message"}`` error result."""
    return ToolCallResult(
        output=json.dumps({"type": error_type, "message": message}),
        is_error=True,
    )


def ok_json(payload: Any) -> ToolCallResult:
    """Build a success result from a plain JSON-compatible payload."""
    return ToolCallResult(output=json.dumps(payload, default=str), is_error=False)


def to_json(payload: Any) -> str:
    """Serialise a payload that may be a Pydantic model or list of models.

    A :class:`BaseModel` serialises via ``model_dump_json()``; a list
    serialises each element (models via ``model_dump(mode="json")``);
    everything else falls back to ``json.dumps(..., default=str)``.
    """
    if isinstance(payload, BaseModel):
        return payload.model_dump_json()
    if isinstance(payload, list):
        return json.dumps(
            [
                p.model_dump(mode="json") if isinstance(p, BaseModel) else p
                for p in payload
            ],
            default=str,
        )
    return json.dumps(payload, default=str)


def ok(payload: Any) -> ToolCallResult:
    """Build a model-aware success result (see :func:`to_json`)."""
    return ToolCallResult(output=to_json(payload), is_error=False)


__all__ = ["err", "ok", "ok_json", "to_json"]
