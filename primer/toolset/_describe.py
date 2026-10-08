"""The single construction path for LLM-facing tool descriptions.

`render_description` composes the final string (Purpose + When + Examples).
`make_tool` validates every example against the tool's JSON Schema at import
time, so a wrong example crashes on module load rather than in production.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal

from jsonschema import Draft202012Validator

from primer.common.preview_paths import missing_paths, path_syntax_error
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
    yields: bool = False,
    requires_workspace: bool = False,
    required_role: str | None = None,
    tool_class: Literal["standard", "notifying"] = "standard",
    interruptible: bool = True,
    preview_args: Sequence[str] | None = None,
) -> Tool:
    """Build a Tool with validated examples and the standard description anatomy.

    ``purpose`` is one imperative sentence; ``when`` starts with "Use when".
    Each example's ``args`` is validated against ``args_schema`` (which must be
    a self-contained JSON Schema) and rejected on mismatch.

    ``yields`` and ``requires_workspace`` are explicit capability flags that
    replace the previous source-introspection heuristics in
    :mod:`primer.toolset.internal`. Set ``yields=True`` when the tool's
    handler can park the agent turn (its return annotation includes
    :class:`primer.model.yield_.Yielded` / it raises ``YieldToWorker``).
    Set ``requires_workspace=True`` when the handler needs a live workspace
    (it reads ``ctx.workspace_id`` for file I/O); such tools are dropped
    from chat tool context and are not MCP-exposable. Both default
    to ``False`` and surface via :meth:`InternalToolsetProvider.is_yielding`
    / :meth:`InternalToolsetProvider.requires_workspace`, which the chat
    suppression choke point and the MCP exposure guard consult.

    ``interruptible`` says whether a Stop may CANCEL a running call of this
    tool (default True). Declare ``interruptible=False`` when the handler
    performs two or more durable writes across stores or rows with no
    transaction, or writes inside a lock that outlives the await: cancelling it
    could leave the work half-done, so the loop waits for the call (a few
    seconds) and records its real result instead. When unsure, a mutator is NOT
    interruptible (the cost is a Stop that waits a few seconds; the opposite
    mistake leaves a half-done write).

    ``preview_args`` declares the dotted paths into the arguments that an approval card may show (the Inbox preview allowlist,
    ``primer/common/preview_paths.py``); every other argument is withheld from the card. ``None`` declares nothing (only arguments whose
    schema is a closed set are shown); ``()`` shows no value. Each path is checked against ``args_schema`` here, like the examples: a path
    that names nothing is a typo, and a typo hides more than its author meant.
    """
    validator = Draft202012Validator(args_schema)
    for ex in examples:
        validator.validate(ex.args)
    declared: tuple[str, ...] | None = None
    if preview_args is not None:
        declared = tuple(preview_args)
        for path in declared:
            problem = path_syntax_error(path)
            if problem is not None:
                raise ValueError(f"tool {id!r}: {problem}")
        gone = missing_paths(args_schema, declared)
        if gone:
            names = sorted(args_schema.get("properties") or {})
            raise ValueError(f"tool {id!r}: preview_args {gone} name no argument of its schema (top-level arguments: {names})")
    body = f"{purpose}\n\n{when}"
    return Tool(
        id=id,
        toolset_id=toolset_id,
        description=render_description(body, examples),
        args_schema=args_schema,
        examples=examples,
        yields=yields,
        requires_workspace=requires_workspace,
        required_role=required_role,
        tool_class=tool_class,
        interruptible=interruptible,
        preview_args=declared,
    )
