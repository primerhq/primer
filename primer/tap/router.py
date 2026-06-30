"""Process-local fan-out of session ticks to per-workspace subscribers.

Spec: ``docs/superpowers/specs/2026-06-30-workspace-tap-design.md`` §2.5.

:class:`WorkspaceTapRouter` consumes ``session:{sid}:tick`` events from
the broadcast event bus and fans them out to subscribers keyed by
*workspace* — resolving each session's ``workspace_id`` through a
cached ``sid -> wid`` lookup against the session store. It owns the
sole ``session:*:tick`` bus subscription now (the per-session WS
fan-out was retired); the durable per-session log is the source of
truth, so a tick is only a "there is new data up to seq N" pointer.

Lifecycle (mirrors the app lifespan):
* Constructed in the app lifespan with the bus + the
  ``Storage[WorkspaceSession]`` handle; stashed on
  ``app.state.workspace_tap_router``.
* :meth:`start` spawns the consume task; :meth:`aclose` cancels it.
* SSE tap handlers call :meth:`subscribe(workspace_id)` to get an
  ``AsyncIterator[WorkspaceTick]`` and ``aclose()`` it on disconnect.

Overflow policy: each subscriber gets a *bounded* queue. On overflow we
**drop the oldest** queued tick (advance past it, enqueue the new one)
rather than blocking the consume loop or dropping the newest. A dropped
tick is safe: it is only a "there is new data up to seq N" pointer, and
the SSE reader catches up from the durable per-session log via its
cursor regardless of which tick woke it. Dropping oldest also means the
most recent (highest) seq is always retained.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.int.event_bus import EventBus
    from primer.int.storage import Storage
    from primer.model.workspace_session import WorkspaceSession


logger = logging.getLogger(__name__)

# Per-subscriber queue bound. Generous enough that a momentarily-slow
# SSE consumer does not lose ticks under normal load, small enough to
# bound memory per connection. On overflow the oldest tick is dropped
# (see module docstring).
_QUEUE_MAXSIZE = 1024

_TICK_PREFIX = "session:"
_TICK_SUFFIX = ":tick"


@dataclass(frozen=True)
class WorkspaceTick:
    """One workspace-scoped tick — new rows exist for ``session_id`` up to ``seq``.

    Carries the originating ``session_id`` so a workspace subscriber can
    tell which session advanced (and read the right per-session log).
    """

    session_id: str
    seq: int


class _Subscription:
    """Async iterator wrapping one workspace subscriber's bounded queue."""

    def __init__(self, queue: asyncio.Queue[WorkspaceTick], on_close) -> None:
        self._queue = queue
        self._on_close = on_close
        self._closed = False

    def __aiter__(self) -> "_Subscription":
        return self

    async def __anext__(self) -> WorkspaceTick:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._on_close(self._queue)


class WorkspaceTapRouter:
    """In-process fan-out of session ticks to per-workspace subscribers.

    Owns a single :class:`EventBus` subscription consumed by an internal
    task. Each ``session:{sid}:tick`` is resolved to a ``workspace_id``
    (cached) and fanned to every subscriber for that workspace.
    """

    def __init__(
        self,
        event_bus: "EventBus",
        session_storage: "Storage[WorkspaceSession]",
    ) -> None:
        self._bus = event_bus
        self._sessions = session_storage
        # workspace_id -> set of subscriber queues
        self._subs: dict[str, set[asyncio.Queue[WorkspaceTick]]] = {}
        # sid -> wid resolution cache (refresh-on-miss from storage)
        self._wid_by_sid: dict[str, str] = {}
        self._sub = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Subscribe to the bus and spawn the consume task. Idempotent."""
        if self._task is not None:
            return
        self._sub = self._bus.subscribe()
        self._task = asyncio.create_task(
            self._consume(), name="workspace-tap-router"
        )

    async def aclose(self) -> None:
        """Cancel the consume task and release the bus subscription. Idempotent."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        sub, self._sub = self._sub, None
        if sub is not None:
            await sub.aclose()

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------
    def subscribe(self, workspace_id: str) -> AsyncIterator[WorkspaceTick]:
        """Return an async iterator of :class:`WorkspaceTick` for ``workspace_id``.

        Caller must ``aclose()`` the returned subscription (e.g. on SSE
        disconnect) so its queue is deregistered.
        """
        queue: asyncio.Queue[WorkspaceTick] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subs.setdefault(workspace_id, set()).add(queue)

        def _deregister(q: asyncio.Queue[WorkspaceTick]) -> None:
            subs = self._subs.get(workspace_id)
            if subs is None:
                return
            subs.discard(q)
            if not subs:
                self._subs.pop(workspace_id, None)

        return _Subscription(queue, _deregister)

    # ------------------------------------------------------------------
    # Consume loop
    # ------------------------------------------------------------------
    async def _consume(self) -> None:
        assert self._sub is not None
        sub = self._sub
        try:
            async for event in sub:
                try:
                    await self._handle(event)
                except Exception:
                    # A single bad event (storage hiccup, unexpected
                    # payload) must never kill the consume loop.
                    logger.exception(
                        "workspace tap router: failed to handle event %r",
                        getattr(event, "event_key", None),
                    )
        except asyncio.CancelledError:
            pass

    async def _handle(self, event) -> None:
        key = event.event_key
        if not key.startswith(_TICK_PREFIX) or not key.endswith(_TICK_SUFFIX):
            return
        sid = key[len(_TICK_PREFIX):-len(_TICK_SUFFIX)]
        if not sid:
            return
        seq = event.payload.get("seq") if event.payload else None
        if not isinstance(seq, int):
            return

        wid = await self._resolve_wid(sid)
        if wid is None:
            return
        self._publish(wid, WorkspaceTick(session_id=sid, seq=seq))

    async def _resolve_wid(self, sid: str) -> str | None:
        """Resolve ``sid -> workspace_id``, caching the result.

        Cache miss → look the session up in storage; a brand-new session
        resolves on its first tick. A missing row or a storage error
        returns ``None`` (the caller skips it) without poisoning the
        cache, so a later successful lookup can still populate it.
        """
        cached = self._wid_by_sid.get(sid)
        if cached is not None:
            return cached
        session = await self._sessions.get(sid)
        if session is None:
            logger.debug(
                "workspace tap router: no session row for sid %r; skipping tick",
                sid,
            )
            return None
        wid = session.workspace_id
        self._wid_by_sid[sid] = wid
        return wid

    def _publish(self, workspace_id: str, tick: WorkspaceTick) -> None:
        """Fan ``tick`` out to every subscriber for ``workspace_id``.

        Non-blocking. On a full subscriber queue we drop the oldest
        queued tick to make room (see module docstring): ticks are
        advisory pointers, the durable log is the source of truth, and a
        slow consumer catches up via its cursor.
        """
        subs = self._subs.get(workspace_id)
        if not subs:
            return
        for q in list(subs):
            try:
                q.put_nowait(tick)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # drop oldest
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(tick)
                except asyncio.QueueFull:
                    # Lost a race with the consumer; safe to drop — the
                    # reader catches up via its cursor.
                    pass


__all__ = ["WorkspaceTapRouter", "WorkspaceTick"]
