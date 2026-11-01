"""Closing a replaced adapter without closing it under the calls that are using it (GX-1).

A registry hands an adapter to a turn and keeps no record of who holds it, so closing it when its row is edited cut every reply that was streaming
from it (the SDK client, the speech stream, a pgvector pool). A replaced adapter is now RETIRED: out of the cache at once, so new requests build a
fresh one, but closed only when no call of its is running and it has been idle for ``idle_seconds`` (a turn holds its adapter between two calls too).

The calls are counted by wrapping the adapter's own coroutine and async-generator methods IN PLACE (instance attributes), so the adapter keeps its
identity and its type; a method that returns a plain value, and an adapter that cannot take instance attributes, are simply not counted and fall
back to the idle period.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

RETIRE_IDLE_SECONDS = 30.0
_STATE_ATTR = "_primer_in_flight"


class _State:
    __slots__ = ("in_flight", "last_activity")

    def __init__(self) -> None:
        self.in_flight = 0
        self.last_activity = time.monotonic()

    def enter(self) -> None:
        self.in_flight += 1
        self.last_activity = time.monotonic()

    def leave(self) -> None:
        self.in_flight -= 1
        self.last_activity = time.monotonic()


def _wrap_coroutine(bound: Callable[..., Any], state: _State) -> Callable[..., Any]:
    @functools.wraps(bound)
    async def call(*args: Any, **kwargs: Any) -> Any:
        state.enter()
        try:
            return await bound(*args, **kwargs)
        finally:
            state.leave()

    return call


def _wrap_async_generator(bound: Callable[..., Any], state: _State) -> Callable[..., Any]:
    @functools.wraps(bound)
    async def stream(*args: Any, **kwargs: Any):
        state.enter()
        inner = bound(*args, **kwargs)
        try:
            async for item in inner:
                yield item
        finally:
            try:
                await inner.aclose()
            finally:
                state.leave()

    return stream


class AdapterRetirement:
    """Counts the running calls of the adapters a registry hands out, and closes the replaced ones once they are idle."""

    def __init__(self, idle_seconds: float = RETIRE_IDLE_SECONDS) -> None:
        self._idle = idle_seconds
        self._retiring: dict[int, tuple[Any, asyncio.Task[None]]] = {}
        self._labels: dict[int, str] = {}

    def track(self, adapter: Any) -> Any:
        """Instrument ``adapter`` in place and return it (the same object)."""
        state = _State()
        try:
            for name in dir(type(adapter)):
                if name.startswith("_") or name == "aclose":
                    continue
                raw = inspect.getattr_static(type(adapter), name, None)
                if not inspect.isfunction(raw):
                    continue
                if inspect.isasyncgenfunction(raw):
                    setattr(adapter, name, _wrap_async_generator(getattr(adapter, name), state))
                elif inspect.iscoroutinefunction(raw):
                    setattr(adapter, name, _wrap_coroutine(getattr(adapter, name), state))
            setattr(adapter, _STATE_ATTR, state)
        except (AttributeError, TypeError):
            pass                                  # an adapter that takes no instance attributes: the idle period alone decides
        return adapter

    def retire(self, adapter: Any, label: str = "") -> None:
        """Take ``adapter`` out of service: it is closed once nothing of it is running and it has been idle for the idle period."""
        key = id(adapter)
        if key in self._retiring:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return                                # no loop to time the close on: the adapter is dropped, as a flush always did
        self._labels[key] = label
        task = loop.create_task(self._close_when_idle(adapter))
        self._retiring[key] = (adapter, task)

    async def _close_when_idle(self, adapter: Any) -> None:
        state: _State | None = getattr(adapter, _STATE_ATTR, None)
        try:
            while True:
                if state is None:
                    await asyncio.sleep(self._idle)
                    break
                quiet = time.monotonic() - state.last_activity
                if state.in_flight == 0 and quiet >= self._idle:
                    break
                await asyncio.sleep(max(self._idle - quiet, 0.01) if state.in_flight == 0 else self._idle)
        except asyncio.CancelledError:
            raise
        await self._close(adapter)

    async def _close(self, adapter: Any) -> None:
        self._retiring.pop(id(adapter), None)
        label = self._labels.pop(id(adapter), "")
        try:
            await adapter.aclose()
        except Exception as exc:  # noqa: BLE001 - best effort: the row write that retired it already landed
            logger.warning("registry: aclose failed on a retired %s %s: %s", type(adapter).__name__, label, exc)

    async def close_all(self) -> None:
        """Close every retired adapter now (the registry is shutting down)."""
        retiring, self._retiring = list(self._retiring.values()), {}
        for _, task in retiring:
            task.cancel()
        for adapter, _ in retiring:
            await self._close(adapter)
