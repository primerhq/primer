"""Jinja renders with a time limit, an output cap and a repetition bound (W24, AG-M23; lead ruling D8).

A sandboxed template is still a program its author controls, and the sandbox bounds neither its time nor its size: two nested
``range(100000)`` loops run for hours, ``'x' * 10**9`` allocates a gigabyte. Subscription payload templates (any FULL grantee of a
trigger) and graph templates render here under three bounds, each refused with a
:class:`~jinja2.exceptions.SecurityError` (a ``TemplateError``, so every caller's existing template-error path reports it):

* time (:data:`RENDER_SECONDS`): a line tracer on the template's own frames raises once the deadline passes, so a runaway render
  stops in its thread instead of being abandoned there still burning CPU;
* output (:data:`RENDER_MAX_CHARS`): the chunks are summed as they stream and the render stops past the cap;
* repetition: ``*`` and ``**`` are intercepted and refused when the result would exceed the cap.

:func:`off_loop` runs a render in a worker thread, with an asyncio backstop for one long C call the tracer cannot interrupt.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from jinja2 import Template
from jinja2.exceptions import SecurityError
from jinja2.runtime import Context
from jinja2.sandbox import SandboxedEnvironment

RENDER_SECONDS = 10.0
RENDER_MAX_CHARS = 1024 * 1024

T = TypeVar("T")


def _time_limit_error() -> SecurityError:
    return SecurityError(f"template render exceeded the {RENDER_SECONDS:g} s time limit")


class BoundedSandboxedEnvironment(SandboxedEnvironment):
    """The sandbox, with ``*`` and ``**`` refused past the output cap (intercepted ops are also never constant-folded at compile time)."""

    intercepted_binops = frozenset({"*", "**"})

    def call_binop(self, context: Context, operator: str, left: Any, right: Any) -> Any:
        if _too_big(operator, left, right):
            raise SecurityError(f"template {operator!r} result exceeds the {RENDER_MAX_CHARS} character limit")
        return super().call_binop(context, operator, left, right)


def _too_big(operator: str, left: Any, right: Any) -> bool:
    max_bits = 8 * RENDER_MAX_CHARS
    ints = isinstance(left, int) and isinstance(right, int)
    if operator == "**":
        return ints and right > 0 and abs(left) > 1 and abs(left).bit_length() * right > max_bits
    if ints:
        return left.bit_length() + right.bit_length() > max_bits
    for seq, times in ((left, right), (right, left)):
        if isinstance(seq, (str, bytes, list, tuple)) and isinstance(times, int) and len(seq) * times > RENDER_MAX_CHARS:
            return True
    return False


def render_bounded(template: Template, scope: Mapping[str, Any]) -> str:
    """``template.render(**scope)`` under the time limit and the output cap."""
    deadline = time.monotonic() + RENDER_SECONDS
    template_file = template.root_render_func.__code__.co_filename

    def on_line(frame: Any, event: str, arg: Any) -> Any:
        if event == "line" and time.monotonic() > deadline:
            raise _time_limit_error()
        return on_line

    def on_call(frame: Any, event: str, arg: Any) -> Any:
        return on_line if frame.f_code.co_filename == template_file else None

    parts: list[str] = []
    size = 0
    previous = sys.gettrace()
    sys.settrace(on_call)
    try:
        for chunk in template.generate(**scope):
            size += len(chunk)
            if size > RENDER_MAX_CHARS:
                raise SecurityError(f"template output exceeds the {RENDER_MAX_CHARS} character limit")
            parts.append(chunk)
    finally:
        sys.settrace(previous)
    return "".join(parts)


async def off_loop(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """``fn(*args, **kwargs)`` (a bounded render) in a worker thread; refused a second past the time limit whatever it is doing."""
    backstop = asyncio.timeout(RENDER_SECONDS + 1)
    try:
        async with backstop:
            return await asyncio.to_thread(fn, *args, **kwargs)
    except TimeoutError:
        if backstop.expired():
            raise _time_limit_error() from None
        raise


__all__ = ["RENDER_MAX_CHARS", "RENDER_SECONDS", "BoundedSandboxedEnvironment", "off_loop", "render_bounded"]
