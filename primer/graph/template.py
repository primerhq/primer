"""Jinja2 template renderer for graph node input templates.

Each :class:`primer.model.graph._AgentNodeRef` (and
:class:`_GraphNodeRef`) carries an ``input_template`` string. The
graph executor renders it against a :class:`GraphContext` to
produce the user-role text that becomes the node's input for that
turn.

Uses :class:`jinja2.sandbox.SandboxedEnvironment` so template
authors can't reach Python internals (no ``__import__`` etc.).
Template syntax errors and missing-attribute errors at render time
both surface as :class:`BadRequestError` so the failure is
attributable to the user-supplied template.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from jinja2 import StrictUndefined, TemplateError
from jinja2.exceptions import TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment

from primer.model.except_ import BadRequestError
from primer.model.graph import GraphContext


logger = logging.getLogger(__name__)


def _fromjson(value: Any) -> Any:
    """``fromjson`` template filter: parse a JSON string into Python data.

    A ``tool_call`` node's ``NodeOutput.parsed`` is only populated when the
    node carries an ``output_schema`` AND the parsed value is a dict, so a
    tool that returns a top-level JSON array (e.g. ``web_search``) leaves
    ``parsed`` as ``None``. This filter lets a downstream node template over
    the JSON ``text`` instead — ``{{ (nodes.search.text | fromjson)[0].url }}``.

    Non-string values (already-parsed dict/list/scalar) pass through
    unchanged, so the filter is idempotent. Invalid JSON raises
    :class:`jinja2.TemplateError` so it surfaces through both render paths as
    a template error (``render_input_template`` → ``BadRequestError``;
    ``render_template_safely`` → ``ended_detail='template_error'``) rather
    than an uncoded crash.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise TemplateError(f"fromjson: invalid JSON: {exc}") from exc
    return value


_FENCE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_FENCE_LINE_RE = re.compile(r"^\s*```")


def _strip_fences(value: Any) -> Any:
    """``strip_fences`` template filter: extract code from markdown fences.

    Local models habitually wrap code in ```lang fenced blocks (and add prose
    around them), which breaks a downstream ``python3``/shell run when the raw
    text is written to a file. This filter sanitises that:

    * If the text contains one or more complete ```...``` blocks, return the
      concatenated block contents (surrounding prose dropped).
    * Otherwise, if a stray/unclosed fence marker line is present, drop those
      marker lines so the remaining text is valid source.
    * Otherwise (already raw), return the text unchanged — idempotent.

    Non-string values pass through unchanged.
    """
    if not isinstance(value, str):
        return value
    blocks = _FENCE_BLOCK_RE.findall(value)
    if blocks:
        return "\n\n".join(b.strip("\n") for b in blocks)
    if "```" in value:
        kept = [ln for ln in value.splitlines() if not _FENCE_LINE_RE.match(ln)]
        return "\n".join(kept)
    return value


# Module-level shared environment. Sandboxed = blocks access to
# dunder attributes / dangerous built-ins; StrictUndefined = raises
# on missing attributes rather than silently emitting empty strings
# (a typo in the user's template should surface immediately).
_ENV: SandboxedEnvironment = SandboxedEnvironment(
    undefined=StrictUndefined,
    autoescape=False,  # rendering plain text, not HTML
    trim_blocks=False,
    lstrip_blocks=False,
)
# Custom filters. ``fromjson`` parses a JSON string (e.g. a tool_call node's
# ``text`` output) so downstream nodes can index into it; see ``_fromjson``.
_ENV.filters["fromjson"] = _fromjson
# ``strip_fences`` extracts code from markdown fences a model may add around
# generated source, so a write-then-exec node gets clean content; see
# ``_strip_fences``.
_ENV.filters["strip_fences"] = _strip_fences


def render_input_template(
    template: str,
    *,
    context: GraphContext,
    extra_scope: dict[str, Any] | None = None,
) -> str:
    """Render ``template`` against ``context``; return the rendered string.

    Variables exposed to the template:

    * ``initial_input`` -- the graph's :attr:`GraphContext.initial_input`
      (a list of :class:`Message` objects).
    * ``iteration`` -- the current graph iteration (int).
    * ``nodes`` -- :attr:`GraphContext.nodes` (a dict keyed by node id;
      values are :class:`NodeOutput` instances). Templates access
      ``nodes.A.text`` / ``nodes.A.parsed`` via attribute syntax.

    ``extra_scope`` (when supplied) merges into the Jinja namespace —
    used by the executor's per-fan-out-instance render path to expose
    ``fanout_index`` and ``fanout_item`` (Spec B §2.1).

    Raises :class:`BadRequestError` on template syntax errors and on
    runtime errors (missing variable, bad attribute, sandbox
    violation). The exception message embeds the original Jinja2
    error so debugging the user's template stays easy.
    """
    try:
        compiled = _ENV.from_string(template)
    except TemplateSyntaxError as exc:
        raise BadRequestError(
            f"render_input_template: template syntax error at line {exc.lineno}: {exc.message}"
        ) from exc
    scope: dict[str, Any] = {
        "initial_input": context.initial_input,
        "iteration": context.iteration,
        "nodes": context.nodes,
        "ctx": context.ctx,
    }
    if extra_scope:
        scope.update(extra_scope)
    try:
        return compiled.render(**scope)
    except TemplateError as exc:
        raise BadRequestError(
            f"render_input_template: render error: {exc!s}"
        ) from exc


def render_template_safely(
    template: str,
    context: GraphContext,
    *,
    extra_scope: dict[str, Any] | None = None,
) -> str:
    """Render ``template`` against ``context`` using the same sandbox /
    StrictUndefined surface as :func:`render_input_template`, but raise
    the underlying :class:`jinja2.TemplateError` rather than wrapping
    it in :class:`BadRequestError`.

    Callers (e.g. End-node output rendering) need to differentiate
    template errors from other failure codes per spec §5.4. Letting the
    raw Jinja exception propagate gives them the choice.

    ``extra_scope`` (when supplied) merges into the Jinja namespace —
    used by the executor's per-fan-out-instance render path to expose
    ``fanout_index`` and ``fanout_item`` (Spec B §2.1). Backwards
    compatible: existing callers that don't pass ``extra_scope``
    continue to render the same scope as before.
    """
    compiled = _ENV.from_string(template)
    scope: dict[str, Any] = {
        "initial_input": context.initial_input,
        "iteration": context.iteration,
        "nodes": context.nodes,
        "ctx": context.ctx,
    }
    if extra_scope:
        scope.update(extra_scope)
    return compiled.render(**scope)


__all__ = ["render_input_template", "render_template_safely"]
