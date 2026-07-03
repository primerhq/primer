"""Streaming-executor turn adapters for the worker pool.

Extracted verbatim from :mod:`primer.worker.pool` (no behaviour change).
These are pure adapter classes — they take an executor at construction and
carry NO reference to the :class:`~primer.worker.pool.WorkerPool`, so unlike
the other extracted helpers they need no ``pool`` argument.

The workspace executors expose ``invoke`` as an async generator (yielding
:class:`StreamEvent`s), but the pool's legacy ``_run_one_turn`` path drives
turns via ``await executor.invoke([])`` — a single-shot coroutine. These
adapters bridge the two by draining the generator to completion inside an
awaitable ``invoke`` and surfacing ``last_done_reason`` for the post-turn
status mapper.

Re-exported from ``primer.worker.pool`` so existing importers
(``primer.worker.executor_builders``, ``tests/worker/test_pool.py``) keep
resolving ``primer.worker.pool._TurnDriver`` / ``._GraphTurnDriver``.
"""

from __future__ import annotations


class _TurnDriver:
    """Adapter that consumes a streaming executor for the turn-based pool.

    The workspace executors expose ``invoke`` as an async generator
    (yielding :class:`StreamEvent`s), but the pool dispatches turns via
    ``await executor.invoke([])`` -- a single-shot coroutine. This adapter
    bridges the two: drain the generator to completion inside an awaitable
    ``invoke`` and surface :attr:`last_done_reason` from the underlying
    executor for the post-turn status mapper to read.
    """

    def __init__(self, executor) -> None:
        self._executor = executor

    @property
    def last_done_reason(self) -> str | None:
        return getattr(self._executor, "last_done_reason", None)

    @property
    def session(self):
        # Pass-through to support tests / introspection that want the
        # underlying :class:`AgentSession`.
        return getattr(self._executor, "session", None)

    async def invoke(self, messages, *, response_format=None) -> None:
        """Drain the executor's stream to completion.

        Events are intentionally discarded here -- streaming-tap
        subscribers attached via :meth:`_BaseAgentExecutor.subscribe`
        still receive them via the executor's own fan-out.
        """
        async for _ev in self._executor.invoke(
            messages, response_format=response_format
        ):
            pass


class _GraphTurnDriver:
    """Adapter for :class:`primer.graph.workspace_executor.WorkspaceGraphExecutor`.

    Two differences from :class:`_TurnDriver`:

    * Graph executor's ``invoke(messages)`` does NOT accept a
      ``response_format`` kwarg (per-node response_format lives on
      each agent node), so this adapter discards the kwarg the worker
      passes uniformly.
    * The graph executor runs the WHOLE graph in one ``invoke()`` call
      (multiple supersteps complete internally before returning), so
      ``last_done_reason`` is a fixed ``"graph_ended"`` sentinel that
      :meth:`WorkerPool._infer_post_turn_status` recognises as ENDED
      — the session is never re-enqueued.
    """

    def __init__(self, executor) -> None:
        self._executor = executor

    @property
    def last_done_reason(self) -> str:
        return "graph_ended"

    @property
    def session(self):
        # Graphs have no single AgentSession in Phase 1; expose None
        # so the pool's introspection callers see a consistent shape.
        return getattr(self._executor, "_workspace_session", None)

    async def invoke(self, messages, *, response_format=None) -> None:
        """Drain the graph executor's stream to completion.

        ``response_format`` is accepted for signature compatibility
        with :class:`_TurnDriver` and silently discarded — graph nodes
        carry their own per-node ``response_format`` on the
        :class:`_AgentNodeRef` model.
        """
        async for _ev in self._executor.invoke(messages):
            pass
