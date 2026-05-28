"""Sweeper is now a no-op — lease-loss is handled by ClaimEngine."""

from __future__ import annotations

from datetime import timedelta

import pytest

from matrix.chat.dispatch import sweep_chats


@pytest.mark.asyncio
async def test_sweep_is_noop(fake_storage_provider, fake_provider_registry):
    """sweep_chats is a legacy shim that always returns 0.

    Claim expiry and worker-death reclaim are now handled by the
    ClaimEngine heartbeat loop in the worker pool.
    """
    reclaimed = await sweep_chats(
        storage_provider=fake_storage_provider,
        scheduler=None,
        event_bus=None,
        heartbeat_stale_after=timedelta(seconds=90),
    )
    assert reclaimed == 0
