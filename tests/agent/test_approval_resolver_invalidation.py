"""GS-28: a policy edit reaches the approval gate's cache in this process at once and in every other process through the bus."""

from __future__ import annotations

from types import SimpleNamespace

from primer.agent.approval import ApprovalResolver
from primer.coordinator.in_memory import InMemoryInvalidationBus


class _Storage:
    def __init__(self):
        self.finds = 0

    async def find(self, predicate, page):
        self.finds += 1
        return SimpleNamespace(items=[])


async def _resolve(resolver):
    await resolver.find(toolset_id="ts", tool_name="t")


async def test_next_resolve_in_the_same_process_sees_an_edit():
    storage = _Storage()
    resolver = ApprovalResolver(storage)
    await _resolve(resolver)
    await _resolve(resolver)
    assert storage.finds == 1
    await resolver.invalidate_everywhere()
    await _resolve(resolver)
    assert storage.finds == 2


async def test_an_edit_in_one_process_drops_the_cache_of_another():
    bus = InMemoryInvalidationBus()
    editor, gate = _Storage(), _Storage()
    editing, gating = ApprovalResolver(editor), ApprovalResolver(gate)
    await editing.bind_invalidation_bus(bus)
    await gating.bind_invalidation_bus(bus)
    await _resolve(gating)
    await editing.invalidate_everywhere()
    await _resolve(gating)
    assert gate.finds == 2
