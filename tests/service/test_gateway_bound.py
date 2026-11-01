"""T107 (GS-8): the gateway runs at most a bounded number of python calls at once; the rest answer busy instead of queueing."""

from __future__ import annotations

import asyncio

import pytest

from primer.model.service import ServiceFunctionSpec, ServiceManifest, ServiceVersion
from primer.service import gateway
from primer.toolset.python_runner.protocol import ShimResponse


class _CountingRunner:
    def __init__(self):
        self.in_flight = 0
        self.peak = 0
        self.release = asyncio.Event()

    async def run(self, request, *, timeout_seconds):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await self.release.wait()
        self.in_flight -= 1
        return ShimResponse(ok=True, value=1)


def _version(service_id: str = "service-1") -> ServiceVersion:
    return ServiceVersion(
        service_id=service_id, version=1, manifest=ServiceManifest(), files={"functions.py": "a1"},
        functions=[ServiceFunctionSpec(name="f", schema={"type": "object"}, timeout_seconds=30, source_file="functions.py")],
    )


async def _burst(runner, count, service_id="service-1"):
    version = _version(service_id)
    calls = [asyncio.create_task(gateway.call_function(service_id=service_id, version=version, source="", fn="f", args={},
                                                       runner=runner)) for _ in range(count)]
    for _ in range(50):
        await asyncio.sleep(0)
    runner.release.set()
    return await asyncio.gather(*calls, return_exceptions=True)


@pytest.fixture(autouse=True)
def _restore_limits():
    yield
    gateway.configure_limits(global_cap=gateway.DEFAULT_GLOBAL_CAP, per_service_cap=None)


async def test_a_burst_runs_at_most_the_cap_and_the_rest_are_refused():
    gateway.configure_limits(global_cap=4, per_service_cap=None)
    runner = _CountingRunner()
    results = await _burst(runner, 64)
    assert runner.peak == 4
    assert sum(1 for r in results if r == 1) == 4
    assert sum(1 for r in results if isinstance(r, gateway.GatewayBusy)) == 60


async def test_the_per_service_cap_stops_one_service_taking_every_slot():
    gateway.configure_limits(global_cap=10, per_service_cap=2)
    runner = _CountingRunner()
    results = await _burst(runner, 6)
    assert runner.peak == 2 and sum(isinstance(r, gateway.GatewayBusy) for r in results) == 4


async def test_slots_are_released_after_each_call():
    gateway.configure_limits(global_cap=1, per_service_cap=None)
    for _ in range(3):
        runner = _CountingRunner()
        runner.release.set()
        assert await gateway.call_function(service_id="service-1", version=_version(), source="", fn="f", args={}, runner=runner) == 1


async def test_a_call_that_raises_still_releases_its_slot():
    gateway.configure_limits(global_cap=1, per_service_cap=None)

    class _Boom:
        async def run(self, request, *, timeout_seconds):
            raise OSError("no process")

    with pytest.raises(gateway.RunnerUnavailable):
        await gateway.call_function(service_id="service-1", version=_version(), source="", fn="f", args={}, runner=_Boom())
    runner = _CountingRunner()
    runner.release.set()
    assert await gateway.call_function(service_id="service-1", version=_version(), source="", fn="f", args={}, runner=runner) == 1
