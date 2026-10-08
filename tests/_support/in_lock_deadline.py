"""Fire ``mutation_lock.IN_LOCK_IO_TIMEOUT_S`` exactly when the code under test is blocked on a hung write, with no wall-clock in the test.

The in-lock writers (the binding route, the checkpoint switch) bound ALL their workspace I/O with one ``asyncio.timeout(IN_LOCK_IO_TIMEOUT_S)``.
A test that shortens it to a fraction of a second and then hangs the write races the code's own earlier steps against that fraction: on a
loaded runner a legitimate step before the hang can use up the budget and the deadline fires somewhere the test did not mean, or the test's own
outer ``wait_for`` runs out first (tests/api/test_session_binding_reservation.py failed that way in CI on unrelated PRs).

Here the seam is set to a SENTINEL value that never elapses, and :meth:`InLockDeadline.hang` expires the deadline that is open for it at the
moment the write hangs: the code under test sees the same ``TimeoutError`` out of the real ``asyncio.timeout`` it would see after
``IN_LOCK_IO_TIMEOUT_S`` seconds, at a point the test chooses.

A test that uses it MUST end with :meth:`InLockDeadline.assert_fired`. That is what proves the code under test really bounded its I/O by the seam:
a caller that does not read the seam opens no sentinel deadline, so ``hang`` finds nothing to expire. ``hang`` then records the misuse and raises
:class:`InLockDeadlineMisuse`, a ``BaseException`` so an ``except Exception`` in the code under test (``apply_queued_binding_switch`` has one
around everything, and swallows the error and carries on) cannot hide it, and ``assert_fired`` fails on the record in any case.
"""

from __future__ import annotations

import asyncio

import pytest

SENTINEL_S = 12345.678


class InLockDeadlineMisuse(BaseException):
    """``hang`` was reached with no deadline of the seam's value open. A ``BaseException`` on purpose: see the module docstring."""


class _Tracked:
    """The context ``asyncio.timeout`` returns, plus whether it is open right now (a deadline that was exited is no longer one to expire)."""

    def __init__(self, owner: "InLockDeadline", inner: asyncio.Timeout) -> None:
        self._owner = owner
        self._inner = inner

    async def __aenter__(self):
        self._owner._active.append(self)
        return await self._inner.__aenter__()

    async def __aexit__(self, *exc_info):
        try:
            return await self._inner.__aexit__(*exc_info)
        finally:
            self._owner._active.remove(self)

    def reschedule(self, when: float | None) -> None:
        self._inner.reschedule(when)


class InLockDeadline:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import primer.session.mutation_lock as mutation_lock

        monkeypatch.setattr(mutation_lock, "IN_LOCK_IO_TIMEOUT_S", SENTINEL_S)
        real_timeout = asyncio.timeout
        self._active: list[_Tracked] = []
        self.fired = False
        self.misused: list[str] = []

        def timeout(delay):
            context = real_timeout(delay)
            return _Tracked(self, context) if delay == SENTINEL_S else context

        monkeypatch.setattr(asyncio, "timeout", timeout)

    async def hang(self) -> None:
        """Never return: expire the deadline that bounds the caller, then wait for the cancellation it delivers."""
        if not self._active:
            message = "hang() was reached with no deadline of the in-lock seam's value open: the code under test does not read IN_LOCK_IO_TIMEOUT_S"
            self.misused.append(message)
            raise InLockDeadlineMisuse(message)
        self._active[-1].reschedule(asyncio.get_running_loop().time())
        self.fired = True
        await asyncio.Event().wait()

    def assert_fired(self) -> None:
        """The hung write really expired a deadline the code under test had opened on the seam (and was never reached without one)."""
        assert not self.misused, f"the code under test did not bound its I/O by the seam: {self.misused}"
        assert self.fired, "no write ever hung: the test did not reach the point it exists to exercise"
