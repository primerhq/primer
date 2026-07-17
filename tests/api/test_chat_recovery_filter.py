"""Unit test for SQL-filtered chat recovery (E-I1).

``recover_chats`` must push the eligibility filter into the storage query
(status='active' AND turn_status IN {claimable, running}) via ``find()``
instead of listing every chat and filtering in Python via ``list()``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.storage import (
    FieldRef,
    OffsetPageResponse,
    Op,
    Predicate,
    Value,
)


class _SpyChatStorage:
    """Records find()/list() calls; returns the seeded items from find()."""

    def __init__(self, items):
        self._items = items
        self.find_calls: list = []
        self.list_calls: list = []

    async def find(self, predicate, page, *, order_by=None):
        self.find_calls.append((predicate, page))
        return OffsetPageResponse(
            offset=0, length=len(self._items),
            total=len(self._items), items=self._items,
        )

    async def list(self, page, *, order_by=None):
        self.list_calls.append(page)
        return OffsetPageResponse(offset=0, length=0, total=0, items=[])


class _SpyStorageProvider:
    def __init__(self, storage):
        self._storage = storage

    def get_storage(self, model_class):
        return self._storage


class _SpyClaimEngine:
    def __init__(self):
        self.upserts: list = []

    async def upsert(self, kind, entity_id):
        self.upserts.append((kind, entity_id))


def _leaf_comparisons(node) -> list[tuple[str, Op, object]]:
    """Flatten an AND/OR predicate tree into its leaf comparisons."""
    if isinstance(node, Predicate) and node.op in (Op.AND, Op.OR):
        return _leaf_comparisons(node.left) + _leaf_comparisons(node.right)
    assert isinstance(node, Predicate)
    assert isinstance(node.left, FieldRef)
    assert isinstance(node.right, Value)
    return [(node.left.name, node.op, node.right.value)]


@pytest.mark.asyncio
async def test_recover_chats_uses_filtered_find_not_list():
    """recover_chats must push status/turn_status into a find() predicate
    (E-I1), never scan the whole table via list()."""
    from primer.api._app_lifespan_phases import recover_chats
    from primer.int.claim import ClaimKind
    from primer.model.chats import Chat

    chat = Chat(
        id="chat-1", agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
        status="active", turn_status="claimable",
    )
    spy_storage = _SpyChatStorage([chat])
    provider = _SpyStorageProvider(spy_storage)
    claim_engine = _SpyClaimEngine()

    await recover_chats(claim_engine, provider)

    # find() used, list() never touched.
    assert len(spy_storage.find_calls) == 1
    assert spy_storage.list_calls == []

    predicate, _page = spy_storage.find_calls[0]
    leaves = _leaf_comparisons(predicate)
    assert ("status", Op.EQ, "active") in leaves
    assert ("turn_status", Op.IN, ["claimable", "running"]) in leaves

    # The returned row is re-armed on the claim engine.
    assert claim_engine.upserts == [(ClaimKind.CHAT, "chat-1")]
