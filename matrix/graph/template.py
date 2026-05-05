"""Jinja2 template renderer for graph node input templates.

Each :class:`matrix.model.graph._AgentNodeRef` (and
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

from jinja2 import StrictUndefined, TemplateError
from jinja2.exceptions import TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment

from matrix.model.except_ import BadRequestError
from matrix.model.graph import GraphContext


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


__all__ = ["render_input_template"]
