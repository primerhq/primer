"""Hold ONE storage write at the write itself, for the race tests of the guarded external-row writers.

The interleaving rule of these tests: the LOSER's write is intercepted, signals that it arrived, and waits on a
two-party barrier BEFORE it is forwarded to the real storage; the test then runs the winner to completion and only
then releases the barrier. The hold has to sit on the WRITE and not on a read: a writer that reads the row after a
read-side pause sees the winner's terminal status and returns early, so the race the test is meant to drive never
happens and an unguarded whole-row write would survive it.

:func:`hold_write` wraps a storage handle's ``update`` and ``patch_if`` and matches either form of the write: a
whole-row ``update(entity)`` whose entity has ``id == row_id`` and ``status == status`` (the shape before the writers
were guarded), or ``patch_if(row_id, patch, ...)`` whose patch sets ``status`` to ``status`` (the guarded shape).
Only the FIRST matching write is held; every other write goes straight through.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any


class HeldWrite:
    """One armed hold. ``held`` names the write that was held (``"update"`` or ``"patch_if"``) once it arrived."""

    def __init__(self, *, row_id: str, status: str) -> None:
        self.row_id = row_id
        self.status = status
        self.held: str | None = None
        self._arrived = asyncio.Event()
        self._barrier = asyncio.Barrier(2)

    def _armed_for_update(self, entity: Any) -> bool:
        return (
            self.held is None
            and getattr(entity, "id", None) == self.row_id
            and getattr(entity, "status", None) == self.status
        )

    def _armed_for_patch(self, row_id: Any, patch: Any) -> bool:
        return (
            self.held is None
            and row_id == self.row_id
            and isinstance(patch, Mapping)
            and patch.get("status") == self.status
        )

    async def _hold(self, kind: str) -> None:
        self.held = kind
        self._arrived.set()
        await self._barrier.wait()

    async def wait_arrived(self, timeout: float = 10.0) -> None:
        """Wait until the loser is parked at its write (fails the test if it never gets there)."""
        await asyncio.wait_for(self._arrived.wait(), timeout)

    async def release(self, timeout: float = 10.0) -> None:
        """The barrier's second party: lets the held write through."""
        await asyncio.wait_for(self._barrier.wait(), timeout)


def hold_write(monkeypatch: Any, storage: Any, *, row_id: str, status: str) -> HeldWrite:
    """Arm a hold on ``storage`` (a ``Storage`` handle shared by every caller) for the first write that sets
    ``row_id`` to ``status``. ``monkeypatch`` undoes the wrapping at teardown."""
    held = HeldWrite(row_id=row_id, status=status)
    real_update = storage.update
    real_patch_if = storage.patch_if

    async def update(entity: Any, *args: Any, **kwargs: Any) -> Any:
        if held._armed_for_update(entity):
            await held._hold("update")
        return await real_update(entity, *args, **kwargs)

    async def patch_if(row_id: str, patch: Any = None, *args: Any, **kwargs: Any) -> Any:
        if held._armed_for_patch(row_id, patch):
            await held._hold("patch_if")
        return await real_patch_if(row_id, patch, *args, **kwargs)

    monkeypatch.setattr(storage, "update", update)
    monkeypatch.setattr(storage, "patch_if", patch_if)
    return held


__all__ = ["HeldWrite", "hold_write"]
