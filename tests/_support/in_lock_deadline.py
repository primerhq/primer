"""Fire ``mutation_lock.IN_LOCK_IO_TIMEOUT_S`` exactly when the code under test is blocked on a hung write, with no wall-clock in the test.

The in-lock writers (the binding route, the checkpoint switch) bound ALL their workspace I/O with one ``asyncio.timeout(IN_LOCK_IO_TIMEOUT_S)``.
A test that shortens it to a fraction of a second and then hangs the write races the code's own earlier steps against that fraction: on a
loaded runner a legitimate step before the hang can use up the budget and the deadline fires somewhere the test did not mean, or the test's own
outer ``wait_for`` runs out first (tests/api/test_session_binding_reservation.py failed that way in CI on unrelated PRs).

Here the seam is set to a SENTINEL value that never elapses, and :meth:`InLockDeadline.hang` expires the deadline that is open for it at the
moment the write hangs: the code under test sees the same ``TimeoutError`` out of the real ``asyncio.timeout`` it would see after
``IN_LOCK_IO_TIMEOUT_S`` seconds, at a point the test chooses. A code path that does not read the seam fails ``hang`` loudly instead of hanging.
"""

from __future__ import annotations

import asyncio

import pytest

SENTINEL_S = 12345.678


class InLockDeadline:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import primer.session.mutation_lock as mutation_lock

        monkeypatch.setattr(mutation_lock, "IN_LOCK_IO_TIMEOUT_S", SENTINEL_S)
        real_timeout = asyncio.timeout
        self.open: list[asyncio.Timeout] = []

        def timeout(delay):
            context = real_timeout(delay)
            if delay == SENTINEL_S:
                self.open.append(context)
            return context

        monkeypatch.setattr(asyncio, "timeout", timeout)

    async def hang(self) -> None:
        """Never return: expire the deadline that bounds the caller, then wait for the cancellation it delivers."""
        assert self.open, "no deadline of the in-lock seam's value was opened: the code under test does not read IN_LOCK_IO_TIMEOUT_S"
        self.open[-1].reschedule(asyncio.get_running_loop().time())
        await asyncio.Event().wait()
