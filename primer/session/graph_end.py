"""The record that ends a graph run (ticket 01a11f35, round 2 of #701).

A graph executor's stream ends with the End node's output and the end node's exit transition: no ``done``, no ``error``. Every node-level terminal carries a ``node_id`` and sits INSIDE the
window (:mod:`primer.session.terminals`), so the graph's turn needs a terminal of its own, and the executor does not write one. The WRITERS do: session dispatch on a graph run's clean completion
(:func:`graph_end_for`) and the graph resume coordinators before they end a resumed graph (``end_graph``: ``resume_graph_engine`` and ``resume_graph_tool_wait``; :func:`graph_end_record`). One
definition of the record, so they cannot disagree. Not every path that ends a graph session writes it (a parked graph cancelled inline, ``resume_engine_session``'s early exits, a log from before it
existed): an ``invocation_divider`` closes the window of a graph run it finds still open, see :mod:`primer.session.terminals`.

It is a node-less ``done`` with ``payload.graph_end`` set (:func:`primer.session.terminals.is_graph_end`): ``stop_reason`` ``stop`` when the graph ended ``completed`` and ``error`` when it did not,
so every reader of a failed turn (the relay, the turn status, the window scanner's copy rule for the claim adapter's release marker) reads a failed graph as it reads any failed turn. It carries
no usage and is not a model call (:func:`primer.session.usage.session_usage` leaves it out of ``model_calls``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord

GRAPH_ENDED = "graph_ended"
GRAPH_FAILED = "graph_failed"


def graph_end_record(*, failed: bool, ended_reason: str | None = None) -> SessionMessageRecord:
    """The node-less ``done`` that ends a graph run; ``failed`` says whether the graph ended anything but ``completed``, ``ended_reason`` how (the executor's, or the row's)."""
    payload: dict[str, Any] = {
        "stop_reason": "error" if failed else "stop",
        "raw_reason": GRAPH_FAILED if failed else GRAPH_ENDED,
        "graph_end": True,
    }
    if ended_reason:
        payload["ended_reason"] = ended_reason
    return SessionMessageRecord(seq=1, kind=SessionMessageKind.DONE, payload=payload, created_at=datetime.now(timezone.utc))        # seq is overwritten by the writer's counter


def graph_end_for(last_done_reason: str | None, executor: Any) -> SessionMessageRecord | None:
    """The end record of a graph run whose stream ended cleanly, or ``None`` for any other turn.

    ``last_done_reason`` is what the executor reports (:attr:`primer.graph.base.GraphExecutor.last_done_reason`): ``"graph_ended"`` or ``"graph_failed"`` for a graph, something else (or nothing)
    for an agent. The executor's own ended reason (``completed``, ``failed``, ``max_iterations``...) rides along when it can be read off the executor or the turn driver that wraps it.
    """
    if last_done_reason not in (GRAPH_ENDED, GRAPH_FAILED):
        return None
    inner = getattr(executor, "_executor", executor)
    reason = getattr(inner, "_last_ended_reason", None)
    return graph_end_record(failed=last_done_reason == GRAPH_FAILED, ended_reason=reason if isinstance(reason, str) else None)


__all__ = ["GRAPH_ENDED", "GRAPH_FAILED", "graph_end_for", "graph_end_record"]
