"""Tests for ApprovalResolver lookup + cache + invalidate."""

from __future__ import annotations

import asyncio

import pytest

from primer.agent.approval import ApprovalResolver
from primer.model.tool_approval import (
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)


class _FakeStorage:
    def __init__(self, rows: list[ToolApprovalPolicy]) -> None:
        self._rows = rows
        self.find_calls = 0

    async def find(self, predicate, page, *, order_by=None):
        self.find_calls += 1
        class _Resp:
            items = [
                r for r in self._rows
                if r.toolset_id == predicate.left.left.right.value
                and r.tool_name == predicate.left.right.right.value
            ]
        return _Resp()


@pytest.mark.asyncio
async def test_resolver_returns_none_when_missing():
    storage = _FakeStorage([])
    r = ApprovalResolver(storage=storage)
    assert await r.find(toolset_id="system", tool_name="x") is None


@pytest.mark.asyncio
async def test_resolver_returns_match():
    policy = ToolApprovalPolicy(
        id="p", toolset_id="system", tool_name="shell_exec",
        approval=RequiredApprovalConfig(),
    )
    storage = _FakeStorage([policy])
    r = ApprovalResolver(storage=storage)
    hit = await r.find(toolset_id="system", tool_name="shell_exec")
    assert hit is not None and hit.id == "p"


@pytest.mark.asyncio
async def test_resolver_caches_within_ttl():
    policy = ToolApprovalPolicy(
        id="p", toolset_id="system", tool_name="shell_exec",
        approval=RequiredApprovalConfig(),
    )
    storage = _FakeStorage([policy])
    r = ApprovalResolver(storage=storage, cache_ttl_seconds=60.0)
    await r.find(toolset_id="system", tool_name="shell_exec")
    await r.find(toolset_id="system", tool_name="shell_exec")
    assert storage.find_calls == 1


@pytest.mark.asyncio
async def test_resolver_invalidate_clears_cache():
    policy = ToolApprovalPolicy(
        id="p", toolset_id="system", tool_name="shell_exec",
        approval=RequiredApprovalConfig(),
    )
    storage = _FakeStorage([policy])
    r = ApprovalResolver(storage=storage, cache_ttl_seconds=60.0)
    await r.find(toolset_id="system", tool_name="shell_exec")
    r.invalidate()
    await r.find(toolset_id="system", tool_name="shell_exec")
    assert storage.find_calls == 2
