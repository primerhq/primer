"""L4: lookups of different tools do not wait on each other, an expired entry is served stale while it refreshes, and an invalidate always wins."""

from __future__ import annotations

import asyncio

import pytest

from primer.agent.approval import ApprovalResolver
from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy


def _policy(tool: str, pid: str = "p") -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id=pid, toolset_id="system", tool_name=tool, approval=RequiredApprovalConfig())


class _GatedStorage:
    """find() waits on a per-tool gate; rows can be swapped to model an edit."""

    def __init__(self):
        self.rows: dict[str, ToolApprovalPolicy] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.calls: list[str] = []

    async def find(self, predicate, page, *, order_by=None):
        tool = predicate.left.right.right.value
        self.calls.append(tool)
        gate = self.gates.get(tool)
        if gate is not None:
            await gate.wait()
        row = self.rows.get(tool)

        class _Resp:
            items = [row] if row is not None else []

        return _Resp()


async def test_a_slow_lookup_of_one_tool_does_not_block_another():
    storage = _GatedStorage()
    storage.rows = {"slow": _policy("slow", "a"), "fast": _policy("fast", "b")}
    storage.gates["slow"] = asyncio.Event()
    resolver = ApprovalResolver(storage)
    slow = asyncio.create_task(resolver.find(toolset_id="system", tool_name="slow"))
    await asyncio.sleep(0.01)
    fast = await asyncio.wait_for(resolver.find(toolset_id="system", tool_name="fast"), 1)
    assert fast.id == "b" and not slow.done()
    storage.gates["slow"].set()
    assert (await slow).id == "a"


async def test_concurrent_cold_lookups_of_one_tool_read_once():
    storage = _GatedStorage()
    storage.rows = {"t": _policy("t")}
    storage.gates["t"] = asyncio.Event()
    resolver = ApprovalResolver(storage)
    tasks = [asyncio.create_task(resolver.find(toolset_id="system", tool_name="t")) for _ in range(5)]
    await asyncio.sleep(0.01)
    storage.gates["t"].set()
    assert [p.id for p in await asyncio.gather(*tasks)] == ["p"] * 5
    assert storage.calls == ["t", "t"]  # one load, which reads each spelling of the toolset id (system, _system) once


async def test_an_expired_entry_is_served_stale_and_refreshed_behind():
    storage = _GatedStorage()
    storage.rows = {"t": _policy("t", "old")}
    resolver = ApprovalResolver(storage, cache_ttl_seconds=0.0)
    assert (await resolver.find(toolset_id="system", tool_name="t")).id == "old"
    storage.rows["t"] = _policy("t", "new")
    storage.gates["t"] = gate = asyncio.Event()  # the refresh read is slow
    served = await asyncio.wait_for(resolver.find(toolset_id="system", tool_name="t"), 1)
    assert served.id == "old"  # did not wait for the read
    gate.set()
    await asyncio.gather(*resolver._refreshing.values())
    assert (await resolver.find(toolset_id="system", tool_name="t")).id == "new"


async def test_invalidate_never_serves_the_dropped_answer_and_an_inflight_read_does_not_refill_it():
    storage = _GatedStorage()
    storage.rows = {"t": _policy("t", "old")}
    resolver = ApprovalResolver(storage)
    await resolver.find(toolset_id="system", tool_name="t")
    storage.rows["t"] = _policy("t", "new")
    resolver.invalidate()
    assert (await resolver.find(toolset_id="system", tool_name="t")).id == "new"

    # a read already out when the invalidate lands may return, but must not be cached
    storage.gates["u"] = gate = asyncio.Event()
    storage.rows["u"] = _policy("u", "u-old")
    inflight = asyncio.create_task(resolver.find(toolset_id="system", tool_name="u"))
    await asyncio.sleep(0.01)
    storage.rows["u"] = _policy("u", "u-new")
    resolver.invalidate()
    gate.set()
    await inflight
    assert (await resolver.find(toolset_id="system", tool_name="u")).id == "u-new"
