"""``tests/_support/in_lock_deadline.InLockDeadline`` proves what it claims (review of #554).

The helper replaces a wall-clock deadline in the tests of the in-lock writers. It is only worth anything if a code path that does NOT bound its
I/O by ``mutation_lock.IN_LOCK_IO_TIMEOUT_S`` cannot pass: ``apply_queued_binding_switch`` swallows every ``Exception`` and carries on, so a
hung write that merely raised inside it used to leave a green test. Pinned here with stand-in callers, not with the production code.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.session import mutation_lock
from tests._support.in_lock_deadline import InLockDeadline, InLockDeadlineMisuse

BODY_BOUND_S = 30.0


async def _bounded_by_the_seam(hang) -> None:
    """What the in-lock writers do: bound the I/O by the seam, and treat a timeout as 'leave it queued'."""
    try:
        async with asyncio.timeout(mutation_lock.IN_LOCK_IO_TIMEOUT_S):
            await hang()
    except TimeoutError:
        return


async def test_a_caller_that_reads_the_seam_gets_its_timeout_and_the_deadline_is_recorded_as_fired(monkeypatch) -> None:
    deadline = InLockDeadline(monkeypatch)

    async with asyncio.timeout(BODY_BOUND_S):
        await _bounded_by_the_seam(deadline.hang)

    deadline.assert_fired()


async def test_a_caller_that_ignores_the_seam_cannot_pass_even_when_it_swallows_every_exception(monkeypatch) -> None:
    """The checkpoint caller's shape: `except Exception` around everything. It opens a deadline of its own (10 s), so no sentinel deadline
    is open when the write hangs."""
    deadline = InLockDeadline(monkeypatch)

    async def swallows_everything() -> None:
        try:
            async with asyncio.timeout(10.0):
                await deadline.hang()
        except Exception:      # noqa: BLE001 - the caller under test does exactly this
            return

    async with asyncio.timeout(BODY_BOUND_S):
        with pytest.raises(InLockDeadlineMisuse):
            await swallows_everything()                # a BaseException: `except Exception` cannot hide it

    with pytest.raises(AssertionError, match="did not bound its I/O by the seam"):
        deadline.assert_fired()                        # and the record fails the test even if something caught that too


async def test_assert_fired_fails_when_no_write_ever_hung(monkeypatch) -> None:
    deadline = InLockDeadline(monkeypatch)

    with pytest.raises(AssertionError, match="no write ever hung"):
        deadline.assert_fired()


async def test_a_deadline_that_was_exited_is_not_the_one_that_gets_expired(monkeypatch) -> None:
    """Two bounded steps in a row: the hang of the second must expire the second's deadline, not an exited first one."""
    deadline = InLockDeadline(monkeypatch)

    async def first_step_then_hang() -> None:
        async with asyncio.timeout(mutation_lock.IN_LOCK_IO_TIMEOUT_S):
            pass                                       # finishes normally; its deadline is exited
        async with asyncio.timeout(mutation_lock.IN_LOCK_IO_TIMEOUT_S):
            await deadline.hang()

    async with asyncio.timeout(BODY_BOUND_S):
        with pytest.raises(TimeoutError):
            await first_step_then_hang()

    deadline.assert_fired()
