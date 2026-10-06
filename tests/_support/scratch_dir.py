"""A scratch directory for an ``asyncio.run`` scenario that is removed AFTER the loop has been shut down.

The pattern this replaces, ``with tempfile.TemporaryDirectory() as tmp:`` INSIDE the coroutine, removes the directory while the
loop is still alive: ``asyncio.run`` has not yet cancelled and awaited the tasks the scenario left behind, closed its async
generators or waited for the default executor's threads (``to_thread`` work cannot be cancelled). A ``git`` write that is
still in flight then creates a file under ``.git`` while ``shutil.rmtree`` walks it, and the removal fails with
``OSError: [Errno 39] Directory not empty: '.git'`` (seen once on a loaded, coverage-instrumented CI runner:
tests/agent/test_tier2_keeps_the_turn.py).

Here the directory is removed once ``asyncio.run`` has returned, so every task and every executor thread of the scenario is done,
and a removal that still meets ENOTEMPTY (a process that is just exiting) is retried a few times before it is a failure.
"""

from __future__ import annotations

import asyncio
import errno
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

#: How many times a removal that meets ENOTEMPTY is tried, and how long it waits between the tries.
REMOVE_ATTEMPTS = 5
REMOVE_DELAY_S = 0.1


def remove_scratch_dir(path: str | Path) -> None:
    """``shutil.rmtree``, retried a bounded number of times when a writer is still adding entries (ENOTEMPTY).

    Any other error is raised at once, and so is ENOTEMPTY once the attempts are used up: a directory that never empties has a
    writer that matters.
    """
    for attempt in range(1, REMOVE_ATTEMPTS + 1):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY or attempt == REMOVE_ATTEMPTS:
                raise
            time.sleep(REMOVE_DELAY_S)


def run_in_scratch_dir(scenario: Callable[[Path], Awaitable[T]], *, prefix: str) -> T:
    """Run ``scenario(root)`` with ``asyncio.run``; remove ``root`` only after the loop is closed, even when it raised."""
    root = tempfile.mkdtemp(prefix=prefix)
    try:
        return asyncio.run(scenario(Path(root)))
    finally:
        remove_scratch_dir(root)
