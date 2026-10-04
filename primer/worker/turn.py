"""Per-turn helpers: cancel scope + retry classification + backoff."""

from __future__ import annotations

import asyncio
from typing import Literal

from primer.model.except_ import TransientError


class _CancelScope:
    """Anchors the current asyncio task so an external caller can cancel it.

    Usage::

        scope = _CancelScope()
        async with scope:
            await something_long()
        # elsewhere:
        scope.cancel("user_pause")

    The cancellation propagates through the awaitable chain via
    :class:`asyncio.CancelledError`. ``msg`` is best-effort: passed to
    ``Task.cancel`` on Python 3.9+; silently dropped on older versions
    (primer targets 3.13 so this fallback is defensive).

    :meth:`cancel` is UNCONDITIONAL: every call delivers another cancel. That is
    what the forced paths need (a lease lost mid-turn, the drain timeout aborting
    a turn stuck unwinding). A USER cancel must use :meth:`cancel_once` instead: a
    second cancel arriving while the session handler is still converging a
    preempted session to ENDED lands inside its ``except CancelledError`` block
    (which awaits storage), skips the convergence, and leaves the session RUNNING
    with its lease dropped. A double-clicked Cancel, or a NOTIFY plus the row
    reconciler both reporting the same cancel, would otherwise do exactly that.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        # Whether a cancel has been delivered, and why the FIRST one was.
        self.cancelled: bool = False
        self.cancel_reason: str | None = None

    async def __aenter__(self) -> "_CancelScope":
        self._task = asyncio.current_task()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def cancel(self, reason: str) -> None:
        """Cancel the anchored task. Every call delivers another cancel."""
        self.cancelled = True
        if self.cancel_reason is None:
            self.cancel_reason = reason
        if self._task is not None and not self._task.done():
            try:
                self._task.cancel(msg=reason)
            except TypeError:
                self._task.cancel()

    def cancel_once(self, reason: str) -> bool:
        """Cancel the anchored task unless a cancel was already delivered.

        Returns True if THIS call cancelled it, False if the scope was already
        cancelled (the turn is unwinding; piling a second cancel on it is the
        strand described above)."""
        if self.cancelled:
            return False
        self.cancel(reason)
        return True


def classify_exception(
    exc: BaseException,
) -> Literal["transient", "cancelled", "fatal"]:
    """Classify a worker-loop exception for the retry decision.

    * ``cancelled``: ``asyncio.CancelledError`` — pause / cancel arrived.
    * ``transient``: ``TransientError`` — retryable adapter failure.
    * ``fatal``:     anything else — end the session as failed.
    """
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, TransientError):
        return "transient"
    return "fatal"


def compute_backoff(*, attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with cap. ``attempt`` is 1-indexed.

    ``compute_backoff(attempt=1, base=2.0, cap=300.0) == 2.0``
    ``compute_backoff(attempt=2, base=2.0, cap=300.0) == 4.0``
    ``compute_backoff(attempt=20, base=2.0, cap=300.0) == 300.0`` (capped)
    """
    return min(base * (2 ** (attempt - 1)), cap)


__all__ = ["_CancelScope", "classify_exception", "compute_backoff"]
