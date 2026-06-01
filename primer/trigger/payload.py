"""Jinja2 sandboxed renderer for subscription payload templates.

Mirrors the harness template renderer in ``primer/harness/template.py``.
"""

from __future__ import annotations
import json
from typing import Any

from jinja2 import StrictUndefined
from jinja2.exceptions import TemplateError as JinjaTemplateError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment


class PayloadTemplateError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
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
        return tmpl.render(**fire_context)
    except UndefinedError as exc:
        raise PayloadTemplateError(str(exc)) from exc
    except JinjaTemplateError as exc:
        raise PayloadTemplateError(str(exc)) from exc
    except Exception as exc:
        raise PayloadTemplateError(str(exc)) from exc


__all__ = ["PayloadTemplateError", "render_payload"]
