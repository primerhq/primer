"""Jinja2 sandboxed renderer for subscription payload templates.

Mirrors the harness template renderer in ``primer/harness/template.py``,
bounded in time, output and repetition (``primer/common/bounded_template.py``).
"""

from __future__ import annotations
import json
from typing import Any

from jinja2 import StrictUndefined
from jinja2.exceptions import TemplateError as JinjaTemplateError, UndefinedError

from primer.common.bounded_template import BoundedSandboxedEnvironment, render_bounded


class PayloadTemplateError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _env() -> BoundedSandboxedEnvironment:
    env = BoundedSandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    env.filters["tojson"] = lambda obj: json.dumps(obj)
    return env


def render_payload(
    template: str | None,
    fire_context: dict[str, Any],
) -> str:
    """Render the subscription's payload template against the fire context.

    If ``template`` is None, return ``json.dumps(fire_context, default=str,
    sort_keys=True)``.
    """
    if template is None:
        return json.dumps(fire_context, default=str, sort_keys=True)
    env = _env()
    try:
        tmpl = env.from_string(template)
        return render_bounded(tmpl, fire_context)
    except UndefinedError as exc:
        raise PayloadTemplateError(str(exc)) from exc
    except JinjaTemplateError as exc:
        raise PayloadTemplateError(str(exc)) from exc
    except Exception as exc:
        raise PayloadTemplateError(str(exc)) from exc


__all__ = ["PayloadTemplateError", "render_payload"]
