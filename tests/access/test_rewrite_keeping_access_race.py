"""``rewrite_keeping_access`` against a sharing change that lands between its read and its write (PERF-T17 L2).

Characterization: the outcomes (the stored sharing is never undone, the rest is written, a vanished row is a NotFound, a sharing that
keeps changing is a Conflict). Perf: a lost race costs ONE storage call (the guarded write answers with the row as it now stands), not a
guarded write plus a second read.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from primer.access.person import Person
from primer.access.service import AccessUpdate, rewrite_keeping_access, set_access
from primer.model.access import Access
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider
from tests.perf.counting import CountingProxy, _Recorder


class Gizmo(Identifiable):
    name: str = "g"
    note: str = ""
    access: Access = Access()


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        for uid in ("u1", "u2", "u3"):
            await provider.get_storage(User).create(
                User(id=uid, username=uid, password_hash=None, created_at=datetime.now(UTC), role="user")
            )
        await provider.get_storage(Gizmo).create(Gizmo(id="g1", access=Access(owners=["u1"], grants={"u2": "full"})))
        yield provider
    finally:
        await provider.aclose()


class Racing:
    """Forwards to a real storage; the first ``races`` guarded writes are preceded by a sharing change (the lost race)."""

    def __init__(self, inner: Any, users: Any, races: int) -> None:
        self._inner, self._users, self.races = inner, users, races
        self._flip = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def _race(self) -> None:
        if self.races > 0:
            self.races -= 1
            self._flip += 1
            grants = {"u2": "full", "u3": "full"} if self._flip % 2 else {"u2": "full"}
            await set_access(self._inner, self._users, "g1", Person("u1", "user"), AccessUpdate(owners=["u1"], grants=grants), what="Gizmo 'g1'")

    async def patch_if(self, *args: Any, **kwargs: Any) -> Any:
        await self._race()
        return await self._inner.patch_if(*args, **kwargs)

    async def patch_if_or_current(self, *args: Any, **kwargs: Any) -> Any:
        await self._race()
        return await self._inner.patch_if_or_current(*args, **kwargs)


async def _edit(store: Any, sp: SqliteStorageProvider, **fields: Any) -> Any:
    existing = await sp.get_storage(Gizmo).get("g1")
    return await rewrite_keeping_access(store, existing.model_copy(update=fields), what="Gizmo 'g1'")


async def test_a_plain_rewrite_writes_the_fields_and_keeps_the_stored_sharing(sp):
    written = await _edit(sp.get_storage(Gizmo), sp, name="renamed", access=Access(owners=["u3"], public=True))
    assert written.name == "renamed"
    assert written.access.owners == ["u1"] and written.access.grants == {"u2": "full"} and not written.access.public


async def test_a_sharing_change_between_read_and_write_is_kept_and_the_write_still_lands(sp):
    racing = Racing(sp.get_storage(Gizmo), sp.get_storage(User), races=1)
    written = await _edit(racing, sp, name="renamed")
    assert written.name == "renamed"
    assert written.access.grants == {"u2": "full", "u3": "full"}, "the owner's grant made in between is not put back"
    assert (await sp.get_storage(Gizmo).get("g1")).access.grants == {"u2": "full", "u3": "full"}


async def test_a_vanished_row_is_not_found(sp):
    await sp.get_storage(Gizmo).delete("g1")
    ghost = Gizmo(id="g1", name="x")
    with pytest.raises(NotFoundError):
        await rewrite_keeping_access(sp.get_storage(Gizmo), ghost, what="Gizmo 'g1'")


async def test_a_sharing_that_keeps_changing_is_a_conflict_and_writes_nothing(sp):
    racing = Racing(sp.get_storage(Gizmo), sp.get_storage(User), races=50)
    with pytest.raises(ConflictError):
        await _edit(racing, sp, name="never")
    assert (await sp.get_storage(Gizmo).get("g1")).name == "g"


async def test_a_lost_race_costs_one_storage_call(sp):
    """Perf (L2): read once, then one guarded write per attempt; the lost attempt hands back the row as it now stands for the retry, so no second read."""
    recorder = _Recorder()
    store = CountingProxy(Racing(sp.get_storage(Gizmo), sp.get_storage(User), races=1), recorder, "g")
    existing = await sp.get_storage(Gizmo).get("g1")
    with recorder.counted() as counts:
        await rewrite_keeping_access(store, existing.model_copy(update={"name": "n"}), what="Gizmo 'g1'")
    assert counts.calls("get") == 1, counts.log
    assert counts.calls("patch_if", "patch_if_or_current") == 2, counts.log  # the lost write and the winning one
    assert counts.total == 3, counts.log
