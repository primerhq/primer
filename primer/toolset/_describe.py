"""The single construction path for LLM-facing tool descriptions.

`render_description` composes the final string (Purpose + When + Examples).
`make_tool` validates every example against the tool's JSON Schema at import
time, so a wrong example crashes on module load rather than in production.
"""
from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from primer.model.chat import Tool, ToolExample


def _compact(args: dict[str, Any]) -> str:
    return json.dumps(args, separators=(",", ":"), ensure_ascii=False)


def render_description(body: str, examples: list[ToolExample]) -> str:
    """Compose ``body`` plus one ``Example:`` line per example."""
    lines = [body.rstrip()]
    for ex in examples:
        line = f"Example: {_compact(ex.args)}"
        if ex.returns:
            line += f" -> {ex.returns}"
        if ex.note:
            line += f"  ({ex.note})"
        lines.append(line)
    return "\n".join(lines)


def make_tool(
    *,
    id: str,
    toolset_id: str,
    purpose: str,
    when: str,
    args_schema: dict[str, Any],
    examples: list[ToolExample],
) -> Tool:
    """Build a Tool with validated examples and the standard description anatomy.

    ``purpose`` is one imperative sentence; ``when`` starts with "Use when".
    Each example's ``args`` is validated against ``args_schema`` (which must be
    a self-contained JSON Schema) and rejected on mismatch.
    """
    validator = Draft202012Validator(args_schema)
    for ex in examples:
        validator.validate(ex.args)
    body = f"{purpose}\n\n{when}"
    return Tool(
        id=id,
        toolset_id=toolset_id,
        description=render_description(body, examples),
        args_schema=args_schema,
        examples=examples,
    )
