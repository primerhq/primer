"""A turn log over a dead connection must not hold a turn's exits (ticket 01a11b58, review of #545, B2).

``WorkspaceTurnLogWriter`` appends (and, on its first append, reads the existing file to find its seq) over the same runtime connection as
the message log. ``safe_append`` swallows errors but not a call that never returns. ``BoundedTurnLogWriter`` gives each call a bound; the
first call that misses it breaks the wrapper for the rest of the turn (later entries are skipped at once, silently), and closing it never
raises, so a ``finally`` that closes the log cannot mask the exception it runs for.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from primer.observability.turn_log_writer import TurnLogWriter
from primer.model.turn_log import TurnLogPhase

HARD_BOUND_S = 5.0
BOUND_S = 0.2


class _Inner(TurnLogWriter):
    def __init__(self, *, hang: bool = False, fail_close: bool = False) -> None:
        self.hang = hang
        self.fail_close = fail_close
        self.appended = 0
        self.closed = 0
        self.release = asyncio.Event()

    async def append(self, event) -> int:
        self.appended += 1
        if self.hang:
            await self.release.wait()
        return self.appended

    async def aclose(self) -> None:
        self.closed += 1
        if self.hang:
            await self.release.wait()
        if self.fail_close:
            raise OSError("close failed")


def _event() -> TurnLogPhase:
    return TurnLogPhase(seq=0, ts=datetime.now(timezone.utc), turn_no=1, phase="thinking")


def _bounded(inner):
    from primer.observability.turn_log_writer import BoundedTurnLogWriter

    return BoundedTurnLogWriter(inner, timeout_s=BOUND_S)


async def test_an_append_that_never_returns_is_given_up_and_the_rest_of_the_turns_entries_are_skipped_at_once() -> None:
    inner = _Inner(hang=True)
    try:
        async with asyncio.timeout(HARD_BOUND_S):
            log = _bounded(inner)
            await log.append(_event())
            await log.append(_event())
            await log.append(_event())
    finally:
        inner.release.set()

    assert inner.appended == 1, "a broken turn log still sent entries over the dead connection"


async def test_a_healthy_turn_log_is_untouched() -> None:
    inner = _Inner()
    async with asyncio.timeout(HARD_BOUND_S):
        log = _bounded(inner)
        assert await log.append(_event()) == 1
        assert await log.append(_event()) == 2
        await log.aclose()

    assert inner.appended == 2 and inner.closed == 1


async def test_closing_never_raises_and_never_hangs() -> None:
    for inner in (_Inner(hang=True), _Inner(fail_close=True)):
        try:
            async with asyncio.timeout(HARD_BOUND_S):
                await _bounded(inner).aclose()
        finally:
            inner.release.set()
        assert inner.closed == 1
