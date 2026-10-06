"""Run cleanup on its own task, with a bounded wait for the caller.

The shape every "undo what an unfinished build left behind" needs: the cleanup must not be abandoned half done by a SECOND
cancel (a drain, a bound landing again) while the caller waits for it, and the caller must not be held past its own bound by a
peer that is slow or silent. ``asyncio.wait`` does not cancel what it waits on, so running the cleanup on its own task and
waiting for it with a timeout gives both: a cancel of the caller leaves the cleanup running to its end, and past the bound the
caller carries on with the error that ended the build while the cleanup finishes in the background.

Used by the workspace backends (``close_shielded``, ``roll_back_shielded`` in ``primer.workspace.base_backend``, which own the
bounds for a close and for a rollback) and by the Discord gateway start (``primer.channel.discord.connection``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable

logger = logging.getLogger(__name__)

#: The cleanups in flight, held so one that outlives its caller (a second cancel, the bound) is not garbage collected before it
#: has finished.
PENDING: "set[asyncio.Future]" = set()


def _finished(task: "asyncio.Future", what: str, verb: str) -> None:
    PENDING.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("%s: %s failed: %s", what, verb, task.exception())


async def run_in_background(work: "Awaitable[None]", *, what: str, verb: str, wait_s: float) -> None:
    """Run ``work`` on its OWN task and wait for it for at most ``wait_s``; never raises what ``work`` raised (it is logged).

    ``asyncio.wait`` does not cancel what it waits on, so a cancel of the caller (a second one, while it waits) leaves the work
    running to its end instead of half done.
    """
    task = asyncio.ensure_future(work)
    PENDING.add(task)
    task.add_done_callback(lambda finished: _finished(finished, what, verb))
    done, _ = await asyncio.wait({task}, timeout=wait_s)
    if not done:
        logger.warning(
            "%s: %s still running after %gs; carrying on without waiting for it", what, verb, wait_s,
        )
