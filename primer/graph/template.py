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

import logging
from typing import Any

from jinja2 import StrictUndefined, TemplateError
from jinja2.exceptions import TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment

from primer.model.except_ import BadRequestError
from primer.model.graph import GraphContext


logger = logging.getLogger(__name__)


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


def render_input_template(
    template: str,
    *,
    context: GraphContext,
) -> str:
    """Render ``template`` against ``context``; return the rendered string.

    Variables exposed to the template:

    * ``initial_input`` -- the graph's :attr:`GraphContext.initial_input`
      (a list of :class:`Message` objects).
    * ``iteration`` -- the current graph iteration (int).
    * ``nodes`` -- :attr:`GraphContext.nodes` (a dict keyed by node id;
      values are :class:`NodeOutput` instances). Templates access
      ``nodes.A.text`` / ``nodes.A.parsed`` via attribute syntax.

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
    try:
        return compiled.render(
            initial_input=context.initial_input,
            iteration=context.iteration,
            nodes=context.nodes,
        )
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
    }
    if extra_scope:
        scope.update(extra_scope)
    return compiled.render(**scope)


__all__ = ["render_input_template", "render_template_safely"]
