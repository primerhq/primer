"""Jinja renderer for agent system prompts (surface-aware via ExecutionContext).

Mirrors the sandbox / StrictUndefined surface of :mod:`primer.graph.template`
so agent system prompts and graph node templates share one Jinja vocabulary.
The ``system_prompt`` list is joined with ``"\n\n"`` (unchanged join semantics)
and rendered against the :class:`ExecutionContext`, exposed to the template as
``ctx`` -- so a prompt can branch on ``ctx.surface`` ("chat" vs "workspace")
and interpolate ``ctx.artifact_dir`` etc.

The Jinja env is intentionally a sibling of the graph node-template env rather
than a shared import: ``agent/`` must not import ``graph/`` (``graph/`` imports
``agent/``). A future refactor may extract a common env factory.
"""

from __future__ import annotations

import json
import re
from typing import Any

from jinja2 import StrictUndefined, TemplateError
from jinja2.exceptions import TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment

from primer.model.except_ import BadRequestError
from primer.model.graph import ExecutionContext


def _fromjson(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise TemplateError(f"fromjson: invalid JSON: {exc}") from exc
    return value


_FENCE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_FENCE_LINE_RE = re.compile(r"^\s*```")


def _strip_fences(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    blocks = _FENCE_BLOCK_RE.findall(value)
    if blocks:
        return "\n\n".join(b.strip("\n") for b in blocks)
    if "```" in value:
        kept = [ln for ln in value.splitlines() if not _FENCE_LINE_RE.match(ln)]
        return "\n".join(kept)
    return value


_ENV: SandboxedEnvironment = SandboxedEnvironment(
    undefined=StrictUndefined,
    autoescape=False,
    trim_blocks=False,
    lstrip_blocks=False,
)
_ENV.filters["fromjson"] = _fromjson
_ENV.filters["strip_fences"] = _strip_fences


def render_system_prompt(
    system_prompt: list[str], ctx: ExecutionContext
) -> str:
    """Join ``system_prompt`` with blank lines and render it against ``ctx``.

    ``ctx`` is exposed to the template as ``ctx``. Raises
    :class:`BadRequestError` on template syntax or render error (e.g. a
    StrictUndefined miss), mirroring :mod:`primer.graph.template`.
    """
    joined = "\n\n".join(system_prompt)
    try:
        compiled = _ENV.from_string(joined)
    except TemplateSyntaxError as exc:
        raise BadRequestError(
            f"render_system_prompt: template syntax error at line "
            f"{exc.lineno}: {exc.message}"
        ) from exc
    try:
        return compiled.render(ctx=ctx)
    except TemplateError as exc:
        raise BadRequestError(
            f"render_system_prompt: render error: {exc!s}"
        ) from exc
