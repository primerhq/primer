"""A small dedicated executor for synchronous token counters.

A local tokenizer is CPU-bound (MEASURED: about 117 ms for 2M characters), so
it must not run on the event loop, and it must not share the process default
thread pool either: the per-turn history parse uses that pool, and a burst of
counts would starve it.

The pool is shared by every session, so a count can wait behind other counts.
"No timeout is needed" holds for the encode itself (finite CPU work on an
already-loaded vocabulary), not for the queue wait; ``run_counter`` therefore
bounds the time a count may sit unstarted and reports
:class:`~primer.model.except_.TokenCounterUnavailable` (transient) past it, so
the wrapper falls back to an estimate instead of letting a turn queue behind
counters.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from primer.model.except_ import TokenCounterUnavailable

T = TypeVar("T")

MAX_WORKERS = 2
DEFAULT_QUEUE_WAIT_S = 2.0

_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def counter_executor() -> ThreadPoolExecutor:
    global _executor  # noqa: PLW0603
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=MAX_WORKERS, thread_name_prefix="primer-count",
            )
        return _executor


def shutdown_counter_executor(*, wait: bool = False) -> None:
    """Stop the counter threads (app teardown; a no-op if no count ever ran).

    ``cancel_futures`` drops counts that have not started; one that is running
    finishes (it is finite CPU work on a loaded vocabulary).
    """
    global _executor  # noqa: PLW0603
    with _lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=True)


async def run_counter(
    fn: Callable[..., T],
    *args: Any,
    queue_wait_s: float = DEFAULT_QUEUE_WAIT_S,
    **kwargs: Any,
) -> T:
    """Run the synchronous counter ``fn`` off the event loop.

    Raises :class:`TokenCounterUnavailable` (transient) if the count has not
    started within ``queue_wait_s``. Whatever ``fn`` raises propagates.

    The job is submitted straight to the executor and cancelled on the CONCURRENT
    future. Cancelling the asyncio future that ``run_in_executor`` returns is not
    enough: it forwards the cancel to the real future from a later loop iteration,
    so a worker that has just been freed can dequeue the abandoned job first and
    burn CPU for nobody (a test caught exactly that, intermittently, on a loaded
    host).
    """
    loop = asyncio.get_running_loop()
    started = asyncio.Event()

    def _run() -> T:
        loop.call_soon_threadsafe(started.set)
        return fn(*args, **kwargs)

    submitted = counter_executor().submit(_run)
    result = asyncio.wrap_future(submitted, loop=loop)
    try:
        await asyncio.wait_for(started.wait(), queue_wait_s)
    except TimeoutError:
        if submitted.cancel():
            raise TokenCounterUnavailable(
                f"token counter queue did not start the count within {queue_wait_s:g}s",
                transient=True,
            ) from None
        # It started in the instant between the timeout and the cancel: it is
        # running now and finite, so take its answer rather than waste it.
    except asyncio.CancelledError:
        submitted.cancel()
        raise
    return await result


__all__ = [
    "DEFAULT_QUEUE_WAIT_S",
    "MAX_WORKERS",
    "counter_executor",
    "run_counter",
    "shutdown_counter_executor",
]
