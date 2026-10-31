"""PERF-T10 (M9) perf: ``enabled_subscriptions`` pushes the enabled flag into the storage predicate, so disabled rows are never read.

The answer (every and only the enabled subscriptions of the trigger, in storage order) is pinned by the
characterization half of this file and stays green through the change. The perf half is RED on the
Python-side filter (250 rows read for 150 enabled) and green once the enabled rows come back in one page.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.trigger import AgentFreshSubConfig, Subscription
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger.dispatch import enabled_subscriptions
from tests.perf.counting import CountingStorageProvider

N = 250
ENABLED = 150


def _sub(i: int, enabled: bool) -> Subscription:
    return Subscription(
        id=f"sub-{i}",
        trigger_id="tr-1",
        config=AgentFreshSubConfig(workspace_id="ws-x", agent_id="ag-x"),
        enabled=enabled,
        created_at=datetime.now(timezone.utc),
    )


async def test_enabled_subscriptions_keeps_every_enabled_row_in_storage_order(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    subs = sp.get_storage(Subscription)
    for i in range(N):
        await subs.create(_sub(i, i < ENABLED))

    storage_order = (await subs.list(OffsetPage(offset=0, length=200))).items + (await subs.list(OffsetPage(offset=200, length=200))).items
    out = await enabled_subscriptions(sp, "tr-1")
    # Same rows and same order as a storage-order walk filtered in Python: the change moves the filter, not the answer.
    assert [s.id for s in out] == [s.id for s in storage_order if s.enabled]
    await sp.aclose()


async def test_enabled_subscriptions_of_250_rows_reads_only_the_enabled_ones(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    subs = sp.get_storage(Subscription)
    for i in range(N):
        await subs.create(_sub(i, i < ENABLED))

    csp = CountingStorageProvider(sp)
    with csp.counted() as c:
        out = await enabled_subscriptions(csp, "tr-1")

    assert len(out) == ENABLED
    # 150 enabled rows fit one 200-row page: one find, 150 rows read. The Python-side filter read all 250 in two pages.
    assert c.calls("find", model="Subscription") == 1
    assert c.rows <= ENABLED
    await sp.aclose()
