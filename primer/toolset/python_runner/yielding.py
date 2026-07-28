"""Yield request to Yielded, with the host owning the routing key.

A tool names a KIND. The host builds the ``event_key`` from the real
ToolContext. This is a security boundary, not tidiness: a function that could
supply ``ask_user:{someone_elses_session}:{their_tcid}`` could resume a park it
does not own, and answer a question asked of another session.
"""

from __future__ import annotations

from typing import Any

from primer.model.yield_ import ToolContext, Yielded

ASK_USER = "ask_user"
TIMER = "timer"
WATCH = "watch"
ALLOWED_KINDS = frozenset({ASK_USER, TIMER, WATCH})


class YieldKindError(ValueError):
    """A yield kind this runner does not route."""


def _coerce_seconds(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def to_yielded(
    yield_request: dict[str, Any],
    *,
    tool_name: str,
    ctx: ToolContext,
    source_version: int,
) -> Yielded:
    """Build the Yielded the tool engine parks on.

    Every interpolated component of the event key comes from ``ctx``. Anything
    in ``yield_request`` that looks like a key is ignored outright.
    """
    kind = str(yield_request.get("kind", ""))
    if kind not in ALLOWED_KINDS:
        raise YieldKindError(
            f"unknown yield kind {kind!r}; expected one of {sorted(ALLOWED_KINDS)}"
        )
    params = yield_request.get("params") or {}

    if kind == ASK_USER:
        event_key = f"ask_user:{ctx.session_id}:{ctx.tool_call_id}"
        timeout = None
    elif kind == TIMER:
        event_key = f"timer:{ctx.tool_call_id}"
        timeout = _coerce_seconds(params.get("seconds"))
    else:
        event_key = f"watch:{ctx.session_id}:{ctx.tool_call_id}"
        timeout = _coerce_seconds(params.get("seconds"))

    return Yielded(
        tool_name=tool_name,
        event_key=event_key,
        timeout=timeout,
        resume_metadata={
            # Pinned so the resume runs the code that parked, not whatever the
            # toolset record says by the time the answer arrives.
            "source_version": source_version,
            "tool_meta": yield_request.get("meta") or {},
            "params": params,
        },
    )
