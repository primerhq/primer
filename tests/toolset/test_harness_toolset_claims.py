"""``harness__fetch`` / ``install`` / ``sync`` / ``uninstall`` enqueue an operation the way the REST routes do (task 01a111d1, D1).

The worker claims harness work only through lease rows: the claim query joins ``leases`` to the harness table and the harness
adapter's eligibility is ``pending_operation IS NOT NULL``; nothing consumes the ``harness-claimable`` bus event. The REST routes
write ``pending_operation``, publish, and then upsert the lease (``ClaimKind.HARNESS``, operator priority). The toolset wrote
``pending_operation`` and published, and never upserted the lease: an operation enqueued by an agent or over MCP stayed pending
forever, and every later operation on that harness answered a conflict.

Making the toolset's operations claimable also makes two other differences from the routes matter. The routes refuse fetch /
install / sync on an outbound harness (409 ``direction_mismatch``) and the toolset did not: with a lease the worker claims the
operation, its own direction guard (``run_one_harness_operation``) fails, and it releases the harness as ERROR, so a
wrong-direction call from an agent would flip the harness to ERROR instead of being answered with a refusal. And the routes
default ``uninstall_cascade`` by direction (an inbound harness removes the objects it installed) while the toolset left the
model default (False: only the harness row and its rendering go).
"""

from __future__ import annotations

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import CLAIM_PRIORITY_OPERATOR, ClaimKind
from primer.model.harness import Harness, HarnessDirection, HarnessOperation, HarnessStatus
from primer.toolset.harness import build_harness_toolset_provider
from tests.toolset.test_harness_toolset import _SP, _EventBus, _make_harness, _result

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": True}

# tool -> (the harness state the tool accepts, the operation it enqueues)
_OPERATIONS = {
    "harness__fetch": (dict(status=HarnessStatus.DRAFT), HarnessOperation.FETCH),
    "harness__install": (dict(status=HarnessStatus.READY, overrides_schema=_SCHEMA), HarnessOperation.INSTALL),
    "harness__sync": (dict(status=HarnessStatus.INSTALLED, available_bundle_hash="h1"), HarnessOperation.SYNC),
    "harness__uninstall": (dict(status=HarnessStatus.DRAFT), HarnessOperation.UNINSTALL),
}


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def event_bus() -> _EventBus:
    return _EventBus()


@pytest.fixture
def engine() -> InMemoryClaimEngine:
    return InMemoryClaimEngine(adapters={})


@pytest.fixture
def toolset(sp, event_bus):
    """No claim engine: the shape every existing test and the API-only mode build."""
    return build_harness_toolset_provider(storage_provider=sp, event_bus=event_bus)


@pytest.fixture
def claiming_toolset(sp, event_bus, engine):
    return build_harness_toolset_provider(storage_provider=sp, event_bus=event_bus, claim_engine=engine)


class TestTheOperationBecomesClaimable:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", sorted(_OPERATIONS))
    async def test_an_enqueued_operation_gets_a_lease_the_worker_can_claim(
        self, tool, claiming_toolset, sp, engine, event_bus
    ) -> None:
        state, operation = _OPERATIONS[tool]
        harness = _make_harness(sp, **state)

        result = await claiming_toolset.call(tool_name=tool, arguments={"id": harness.id})

        assert not result.is_error, result.output
        assert _result(result)["pending_operation"] == operation.value
        assert await engine.has_lease(ClaimKind.HARNESS, harness.id), "enqueued, and nothing will ever claim it"
        claimed = await engine.claim_due("worker-1", max_count=5, kinds=[ClaimKind.HARNESS])
        assert [lease.entity_id for lease in claimed] == [harness.id]
        assert any(key == "harness-claimable" for key, _ in event_bus.published), "the bus event is still published"

    @pytest.mark.asyncio
    async def test_the_lease_has_the_operator_priority_the_routes_give_it(self, claiming_toolset, sp, engine) -> None:
        harness = _make_harness(sp)

        await claiming_toolset.call(tool_name="harness__fetch", arguments={"id": harness.id})

        assert engine._leases[(ClaimKind.HARNESS, harness.id)].priority_score == CLAIM_PRIORITY_OPERATOR  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_a_refused_call_creates_no_lease(self, claiming_toolset, sp, engine) -> None:
        busy = _make_harness(sp, id="hns_busy", slug="busy-one", pending_operation=HarnessOperation.INSTALL)

        refused = await claiming_toolset.call(tool_name="harness__fetch", arguments={"id": busy.id})
        missing = await claiming_toolset.call(tool_name="harness__fetch", arguments={"id": "hns_ghost"})

        assert refused.is_error and missing.is_error
        assert not await engine.has_lease(ClaimKind.HARNESS, busy.id)
        assert not await engine.has_lease(ClaimKind.HARNESS, "hns_ghost")

    @pytest.mark.asyncio
    async def test_without_a_claim_engine_the_operation_is_still_enqueued_and_published(
        self, toolset, sp, event_bus
    ) -> None:
        harness = _make_harness(sp)

        result = await toolset.call(tool_name="harness__fetch", arguments={"id": harness.id})

        assert not result.is_error, result.output
        assert _result(result)["pending_operation"] == "fetch"
        assert any(key == "harness-claimable" for key, _ in event_bus.published)


class TestDirectionIsGuardedLikeTheRoutes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", ["harness__fetch", "harness__install", "harness__sync"])
    async def test_an_inbound_operation_on_an_outbound_harness_is_refused(self, tool, toolset, sp) -> None:
        harness = _make_harness(sp, direction=HarnessDirection.OUTBOUND)

        result = await toolset.call(tool_name=tool, arguments={"id": harness.id})

        assert result.is_error
        assert _result(result)["type"] == "direction-mismatch"
        assert sp.get_storage(Harness)._data[harness.id].pending_operation is None, "the operation was enqueued anyway"

    @pytest.mark.asyncio
    async def test_the_refusal_creates_no_lease(self, claiming_toolset, sp, engine) -> None:
        harness = _make_harness(sp, direction=HarnessDirection.OUTBOUND)

        await claiming_toolset.call(tool_name="harness__fetch", arguments={"id": harness.id})

        assert not await engine.has_lease(ClaimKind.HARNESS, harness.id)

    @pytest.mark.asyncio
    async def test_uninstall_is_allowed_on_an_outbound_harness(self, toolset, sp) -> None:
        """The delete route does not guard direction: an outbound harness is removed too (without its tracked objects)."""
        harness = _make_harness(sp, direction=HarnessDirection.OUTBOUND)

        result = await toolset.call(tool_name="harness__uninstall", arguments={"id": harness.id})

        assert not result.is_error, result.output
        assert _result(result)["pending_operation"] == "uninstall"


class TestUninstallCascadeDefaultsByDirection:
    @pytest.mark.asyncio
    async def test_an_inbound_harness_uninstall_removes_the_objects_it_installed(self, toolset, sp) -> None:
        harness = _make_harness(sp, direction=HarnessDirection.INBOUND)

        await toolset.call(tool_name="harness__uninstall", arguments={"id": harness.id})

        assert sp.get_storage(Harness)._data[harness.id].uninstall_cascade is True

    @pytest.mark.asyncio
    async def test_an_outbound_harness_keeps_the_objects_it_merely_tracks(self, toolset, sp) -> None:
        harness = _make_harness(sp, direction=HarnessDirection.OUTBOUND)

        await toolset.call(tool_name="harness__uninstall", arguments={"id": harness.id})

        assert sp.get_storage(Harness)._data[harness.id].uninstall_cascade is False
