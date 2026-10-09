"""Yield request to Yielded, with the host owning the routing key.

A tool names a KIND. The host builds the ``event_key`` from the real
ToolContext. This is a security boundary, not tidiness: a function that could
supply ``ask_user:{someone_elses_session}:{their_tcid}`` could resume a park it
does not own, and answer a question asked of another session.
"""

from __future__ import annotations

from typing import Any

from primer.model.yield_ import ToolContext, Yielded, timer_event_key

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

    # Inside a graph node the ambient fan-out-instance id is folded into the key, as ``_ask_user_handler`` does: two concurrent siblings that share a raw
    # provider tool_call_id would otherwise wait on ONE key, and an answer to either could not say which it was for (C-033 round 3; the timer key too). Lazy
    # import: primer.graph imports the toolsets transitively at package-init time. None (every non-graph path) keeps the key byte-identical.
    from primer.graph._node_identity import current_graph_node_id

    node_scope = current_graph_node_id()
    if kind == ASK_USER:
        event_key = (
            f"ask_user:{ctx.session_id}:{node_scope}:{ctx.tool_call_id}" if node_scope is not None
            else f"ask_user:{ctx.session_id}:{ctx.tool_call_id}"
        )
        timeout = None
    elif kind == TIMER:
        event_key = timer_event_key(ctx, node_scope)
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
