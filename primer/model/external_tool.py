"""External tool call models (invoker-supplied tools).

``ExternalToolDef`` is wire-only: it rides invocation bodies (session
create/steer, chat send) and is snapshotted onto the session/chat row
for the turn it triggers - it is never a storage entity itself.
``ExternalToolCall`` is the stored, API-facing record of one
pending/resolved call; the park slot remains the execution source of
truth while these rows serve discovery (pending endpoints, the global
list) and audit.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from primer.model.common import Identifiable

MAX_EXTERNAL_TOOLS = 64
MAX_EXTERNAL_TOOLS_BYTES = 256 * 1024

ExternalToolCallStatus = Literal["pending", "completed", "cancelled", "timed_out"]


class ExternalToolDef(BaseModel):
    """One invoker-supplied tool definition (wire model)."""

    # Same alias treatment as primer.model.chat.Tool: Python code uses
    # ``args_schema``; the JSON wire key is ``"schema"`` both ways.
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    name: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description=(
            "Bare tool name. Served to the model as external__<name>; "
            "must not contain the '__' scope separator."
        ),
    )
    description: str = Field(..., min_length=1)
    args_schema: dict[str, Any] = Field(
        ...,
        validation_alias="schema",
        serialization_alias="schema",
        description="JSON Schema for the tool's argument object.",
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Optional deadline for the invoker's response; on expiry the "
            "turn resumes with a synthetic timed-out result."
        ),
    )

    @field_validator("name")
    @classmethod
    def _no_scope_separator(cls, v: str) -> str:
        # The pattern's charset admits "has__dunder"; reject explicitly
        # because the double underscore is the toolset scope separator.
        if "__" in v:
            raise ValueError("tool name must not contain '__' (scope separator)")
        return v

    @field_validator("args_schema")
    @classmethod
    def _valid_json_schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        import jsonschema as _js

        try:
            _js.Draft202012Validator.check_schema(v)
        except _js.SchemaError as exc:
            raise ValueError(f"invalid JSON Schema: {exc.message}") from exc
        return v


def validate_external_tool_defs(defs: list[ExternalToolDef]) -> None:
    """Message-level validation: dup names + count + serialized size caps.

    Raises ``ValueError`` with an operator-readable message; API callers
    translate that into a 422.
    """
    if len(defs) > MAX_EXTERNAL_TOOLS:
        raise ValueError(
            f"too many external tools ({len(defs)}); max is {MAX_EXTERNAL_TOOLS}"
        )
    names = [d.name for d in defs]
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(f"duplicate external tool names: {dupes}")
    total = sum(
        len(json.dumps(d.model_dump(by_alias=True), separators=(",", ":")))
        for d in defs
    )
    if total > MAX_EXTERNAL_TOOLS_BYTES:
        raise ValueError(
            "external_tools payload too large "
            f"({total} bytes; max is {MAX_EXTERNAL_TOOLS_BYTES} = 256 KiB)"
        )


class ExternalToolCall(Identifiable):
    """Stored record of one external tool call (pending or resolved)."""

    _id_prefix: ClassVar[str] = "etool"

    session_id: str | None = Field(default=None)
    chat_id: str | None = Field(default=None)
    node_id: str | None = Field(
        default=None, description="Graph node that raised the call, if any."
    )
    tool_call_id: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ExternalToolCallStatus = Field(default="pending")
    result: Any | None = Field(default=None)
    is_error: bool = Field(default=False)
    created_at: datetime | None = Field(default=None)
    resolved_at: datetime | None = Field(default=None)
    timeout_at: datetime | None = Field(default=None)


__all__ = [
    "MAX_EXTERNAL_TOOLS",
    "MAX_EXTERNAL_TOOLS_BYTES",
    "ExternalToolCall",
    "ExternalToolCallStatus",
    "ExternalToolDef",
    "validate_external_tool_defs",
]
