"""EVENTS_SUBSCRIBE: broadcast workspace lifecycle to any subscriber.

One long-lived streaming op (mirrors WATCH_START: keyed by its req_id,
cancelled on connection close). A subscriber names the kinds it wants:

* ``file_changed`` — served by the existing inotify subscription
  machinery (:func:`primer_runtime.watch.start_watch`) under the same
  req_id, so change frames arrive exactly as the watch op emits them
  (``watch_open`` / ``change`` / ``watch_closed``).
* ``exec_started`` / ``exec_exited`` — served by the app-level
  :class:`EventBroadcaster`: every EXEC op the server runs notifies it,
  regardless of which connection requested the exec, and each matching
  subscriber receives a ``ws_event`` frame.

The broadcaster never raises out of :meth:`EventBroadcaster.broadcast`:
a subscriber whose send fails (connection gone) is dropped.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from primer_runtime.protocol import Event, Response, serialize
from primer_runtime.watch import WatchRegistry, start_watch

log = logging.getLogger(__name__)

EXEC_KINDS = frozenset({"exec_started", "exec_exited"})
ALL_KINDS = EXEC_KINDS | {"file_changed"}


@dataclass
class _Subscriber:
    req_id: int
    kinds: frozenset[str]
    send: Callable[[str], Coroutine[Any, Any, None]]


@dataclass
class EventBroadcaster:
    """App-level fan-out for runtime lifecycle events."""

    _subs: dict[int, _Subscriber] = field(default_factory=dict)
    _ids: "itertools.count[int]" = field(
        default_factory=lambda: itertools.count(1),
    )

    def subscribe(
        self,
        *,
        req_id: int,
        kinds: frozenset[str],
        send: Callable[[str], Coroutine[Any, Any, None]],
    ) -> int:
        handle = next(self._ids)
        self._subs[handle] = _Subscriber(req_id=req_id, kinds=kinds, send=send)
        return handle

    def unsubscribe(self, handle: int) -> None:
        self._subs.pop(handle, None)

    async def broadcast(self, kind: str, data: dict[str, Any]) -> None:
        """Deliver one lifecycle event to every matching subscriber."""
        dead: list[int] = []
        for handle, sub in list(self._subs.items()):
            if kind not in sub.kinds:
                continue
            frame = serialize(Event(
                req_id=sub.req_id,
                event="ws_event",
                data={"kind": kind, **data},
            ))
            try:
                await sub.send(frame)
            except Exception:  # noqa: BLE001 - subscriber gone
                dead.append(handle)
        for handle in dead:
            self.unsubscribe(handle)


async def start_events_subscribe(
    *,
    req_id: int,
    args: dict[str, Any],
    workspace_root: str,
    send: Callable[[str], Coroutine[Any, Any, None]],
    broadcaster: EventBroadcaster,
    watch_registry: WatchRegistry,
) -> int | None:
    """Wire one EVENTS_SUBSCRIBE op; returns the broadcaster handle
    (None when no exec kinds were requested). The caller unsubscribes
    the handle on connection close; the file subscription task is owned
    by ``watch_registry`` and torn down with the connection like any
    watch."""
    raw_kinds = args.get("kinds") or sorted(ALL_KINDS)
    kinds = frozenset(k for k in raw_kinds if k in ALL_KINDS)
    prefixes = args.get("path_prefixes") or ["."]

    handle: int | None = None
    exec_kinds = kinds & EXEC_KINDS
    if exec_kinds:
        handle = broadcaster.subscribe(
            req_id=req_id, kinds=exec_kinds, send=send,
        )
    if "file_changed" in kinds:
        start_watch(
            req_id=req_id,
            args={"paths": list(prefixes)},
            workspace_root=workspace_root,
            send=send,
            registry=watch_registry,
        )
    await send(serialize(Response(
        req_id=req_id, ok=True,
        result={"kinds": sorted(kinds)},
    )))
    return handle


__all__ = [
    "ALL_KINDS",
    "EXEC_KINDS",
    "EventBroadcaster",
    "start_events_subscribe",
]
