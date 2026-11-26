"""A narrow signal from a rate limiter to whoever is running the caller's turn.

A turn that waits for a provider's concurrency slot is not running: it holds a worker slot and does nothing.
The worker pool sets a callback in the turn's task context; a limiter calls it with ``True`` when a wait begins and
``False`` when it ends. The pool uses this to let other work be claimed while slow-provider turns queue
(review 2026-11-23, 1.5). A context variable, not a module-level primitive: it is per task and child tasks inherit it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# Called with True when the current turn starts waiting for a slot and False when the wait ends.
rate_limit_wait_hook: ContextVar[Callable[[bool], None] | None] = ContextVar(
    "primer_rate_limit_wait_hook", default=None,
)


def _signal(hook: Callable[[bool], None], waiting: bool) -> None:
    try:
        hook(waiting)
    except Exception:  # noqa: BLE001 -- advisory: a broken hook must never break an acquire
        logger.exception("rate-limit wait hook failed")


@contextmanager
def waiting_for_slot() -> Iterator[None]:
    """Bracket a limiter's blocking wait. A no-op when no hook is set (any caller outside a worker turn)."""
    hook = rate_limit_wait_hook.get()
    if hook is None:
        yield
        return
    _signal(hook, True)
    try:
        yield
    finally:
        _signal(hook, False)
