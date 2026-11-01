"""Yield request to Yielded, with the host owning the routing key.

A tool names a KIND. The host builds the ``event_key`` from the real
ToolContext. This is a security boundary, not tidiness: a function that could
supply ``ask_user:{someone_elses_session}:{their_tcid}`` could resume a park it
does not own, and answer a question asked of another session.
"""

from __future__ import annotations

from typing import Any

from primer.model.yield_ import ToolContext, Yielded, scoped_event_key, timer_event_key

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
        event_key = scoped_event_key("watch", ctx, node_scope)
        timeout = _coerce_seconds(params.get("seconds"))

    resume_metadata: dict[str, Any] = {
        # Pinned so the resume can tell whether the source changed while the
        # call was parked: a resume on another version is refused
        # (PythonToolsetProvider.resume_tool), never run on the new code.
        "source_version": source_version,
        "tool_meta": yield_request.get("meta") or {},
        "params": params,
    }
    if kind == WATCH:
        # The watcher and the write wake read the park's paths from the TOP of resume_metadata, where the built-in watch_files puts them (GS-12).
        resume_metadata.update(_watch_metadata(params, ctx))
    return Yielded(tool_name=tool_name, event_key=event_key, timeout=timeout, resume_metadata=resume_metadata)


_DEFAULT_BATCH_WINDOW_MS = 250


def _watch_metadata(params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """``paths``, ``batch_window_ms`` and ``workspace_id`` of a python tool's watch, with the built-in ``watch_files`` path rules."""
    paths = params.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths) or len(paths) > 200:
        raise YieldKindError("watch_files needs a list of 1 to 200 workspace-relative path strings")
    for p in paths:
        if p.startswith("/") or (len(p) >= 2 and p[1] == ":"):
            raise YieldKindError(f"absolute paths not allowed: {p!r}")
        if ".." in p.replace("\\", "/").split("/"):
            raise YieldKindError(f"path traversal not allowed: {p!r}")
    if ctx.workspace_id is None:
        raise YieldKindError("watch_files needs a workspace: the watcher resolves the paths in its filesystem")
    window = params.get("batch_window_ms", _DEFAULT_BATCH_WINDOW_MS)
    if isinstance(window, bool) or not isinstance(window, int) or not 0 <= window <= 5000:
        window = _DEFAULT_BATCH_WINDOW_MS
    return {"paths": list(paths), "batch_window_ms": window, "workspace_id": ctx.workspace_id}
