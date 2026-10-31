"""An install racing a revoke must not leave pre-revoke access on a NEW row (access-control P3 SEC, finding 11a).

The row's access is read from the harness inside the install's transaction; a revoke that lands after that read fans out before the row is committed
and misses it. The install therefore reads the harness again once the row is visible and copies what changed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from primer.harness.service import _write_entity_with_body
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.harness import Harness
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

ALICE, BOB = "alice", "bob"
NOW = datetime(2026, 10, 31, tzinfo=UTC)


class _StaleOnce:
    """The harness storage as the install's transaction first sees it: the access from BEFORE the revoke, then the live row."""

    def __init__(self, inner, stale: Harness) -> None:
        self._inner, self._stale, self._reads = inner, stale, 0

    async def get(self, entity_id, **kw):
        self._reads += 1
        return self._stale if self._reads == 1 else await self._inner.get(entity_id, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _Provider:
    def __init__(self, inner, harness_storage) -> None:
        self._inner, self._harness_storage = inner, harness_storage

    def get_storage(self, model):
        return self._harness_storage if model is Harness else self._inner.get_storage(model)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "h.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


async def test_a_new_row_gets_the_revoked_access_not_the_one_the_install_read(sp) -> None:
    before = Access(owners=[ALICE], grants={BOB: "read"}, rev=1)
    after = Access(owners=[ALICE], rev=2)  # bob revoked
    harness = Harness(id="hns_1", slug="acme", name="Acme", git_url="https://github.com/x/y", created_at=NOW, access=after)
    await sp.get_storage(Harness).create(harness)
    provider = _Provider(sp, _StaleOnce(sp.get_storage(Harness), harness.model_copy(update={"access": before})))
    agent = Agent(id="ag-new", description="d", harness_id="hns_1", model={"profile_id": "p--m"}, tools=[], system_prompt=[])

    await _write_entity_with_body(storage_provider=provider, storage=sp.get_storage(Agent), entity=agent, content=None, is_update=False)

    stored = await sp.get_storage(Agent).get("ag-new")
    assert BOB not in stored.access.grants and stored.access == after
