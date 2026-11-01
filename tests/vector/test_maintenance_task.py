"""GI-28: nothing called ``maintain_indexes``; the leader-run task now does, on each provider's ``reindex_cron`` (Lance: nightly)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from primer.vector.maintenance import LANCE_DEFAULT_CRON, VectorMaintenance, cron_of


def _row(ssp_id, kind, cron=None):
    return SimpleNamespace(id=ssp_id, provider=SimpleNamespace(value=kind), config=SimpleNamespace(reindex_cron=cron))


class _Storage:
    def __init__(self, rows):
        self.rows = rows

    async def list(self, page):
        chunk = self.rows[page.offset : page.offset + page.length]
        return SimpleNamespace(items=chunk)


class _Provider:
    def __init__(self, ssp_id, calls):
        self.ssp_id, self.calls = ssp_id, calls

    async def maintain_indexes(self):
        self.calls.append(self.ssp_id)
        return [SimpleNamespace(collection_id="c", action="none")]


class _Registry:
    def __init__(self):
        self.calls: list[str] = []

    async def get_provider(self, ssp_id):
        return _Provider(ssp_id, self.calls)


class _Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def _at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


def _task(rows, clock, registry=None):
    registry = registry or _Registry()
    return VectorMaintenance(storage=_Storage(rows), registry=registry, clock=clock), registry


def test_cron_of_uses_the_config_then_the_lance_default_and_otherwise_none():
    assert cron_of(_row("a", "pgvector", "0 3 * * *")) == "0 3 * * *"
    assert cron_of(_row("b", "lance")) == LANCE_DEFAULT_CRON
    assert cron_of(_row("c", "pgvector")) is None
    assert cron_of(_row("d", "lance", "*/30 * * * *")) == "*/30 * * * *"


async def test_a_provider_runs_once_after_its_cron_time_and_not_before():
    clock = _Clock(_at(1, 1))
    task, registry = _task([_row("pg", "pgvector", "0 3 * * *")], clock)
    assert await task.run_due() == 0  # first sight at 01:00: scheduled for 03:00
    clock.at = _at(1, 2, 59)
    assert await task.run_due() == 0 and registry.calls == []
    clock.at = _at(1, 3, 1)
    assert await task.run_due() == 1 and registry.calls == ["pg"]
    clock.at = _at(1, 3, 30)
    assert await task.run_due() == 0  # once per fire
    clock.at = _at(2, 3, 1)
    assert await task.run_due() == 1 and registry.calls == ["pg", "pg"]


async def test_a_lance_provider_with_no_cron_gets_the_nightly_default():
    clock = _Clock(_at(1, 1))
    task, registry = _task([_row("lance", "lance")], clock)
    await task.run_due()
    clock.at = _at(1, 3, 5)
    await task.run_due()
    assert registry.calls == ["lance"]


async def test_a_pgvector_provider_without_a_cron_is_never_maintained():
    clock = _Clock(_at(1, 1))
    task, registry = _task([_row("pg", "pgvector")], clock)
    for day in range(1, 5):
        clock.at = _at(day, 4)
        await task.run_due()
    assert registry.calls == []


async def test_an_edited_schedule_is_rescheduled_and_a_failing_provider_does_not_skip_the_others():
    clock = _Clock(_at(1, 1))
    rows = [_row("bad", "pgvector", "0 3 * * *"), _row("good", "pgvector", "0 3 * * *")]
    registry = _Registry()
    real = registry.get_provider

    async def get(ssp_id):
        if ssp_id == "bad":
            raise RuntimeError("down")
        return await real(ssp_id)

    registry.get_provider = get
    task, _ = _task(rows, clock, registry)
    await task.run_due()
    clock.at = _at(1, 3, 1)
    assert await task.run_due() == 1 and registry.calls == ["good"]
    rows[1].config.reindex_cron = "0 5 * * *"  # edited: counted from now, not fired at once
    assert await task.run_due() == 0
    clock.at = _at(1, 5, 1)
    assert await task.run_due() == 1


class _Elector:
    def __init__(self, leader: bool):
        self.leader = leader

    async def try_acquire(self, role):
        assert role == "vector-maintenance"
        return SimpleNamespace(lost_event=asyncio.Event(), release=_noop) if self.leader else None


async def _noop():
    return None


async def test_only_the_leader_runs_it():
    for leader in (False, True):
        clock = _Clock(_at(1, 4))
        task, _ = _task([_row("lance", "lance")], clock)
        task._poll = 0.01
        task.start(_Elector(leader))
        await asyncio.sleep(0.15)
        await task.stop()
        assert (task._next != {}) is leader  # a non-leader never even looks at the providers
