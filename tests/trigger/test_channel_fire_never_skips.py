"""S6 P3 / crosscheck M7: channel thread dispatch never busy-skips.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 5. A skip here
would silently drop a user's first message in a brand-new thread.
"""

from __future__ import annotations

from datetime import UTC, datetime

from primer.model.trigger import AgentFreshSubConfig, Subscription
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.trigger.subscribers import DispatchDeps, check_subscription_busy
from tests.conftest import _FakeStorageProvider


def _sub() -> Subscription:
    return Subscription(
        id="sb-1", trigger_id="tr-1",
        config=AgentFreshSubConfig(workspace_id="w1", agent_id="ag1"),
        parallelism="skip", created_at=datetime.now(UTC),
    )


async def _busy_provider():
    sp = _FakeStorageProvider()
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s-live", workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
        turn_no=3, metadata={"subscription_id": "sb-1"},
    ))
    return sp


async def test_time_fire_still_skips_a_busy_subscription():
    sp = await _busy_provider()
    skip = await check_subscription_busy(
        _sub(), DispatchDeps(storage_provider=sp, claim_engine=None),
        fire_context={"kind": "scheduled"},
    )
    assert skip is not None
    assert skip.error_code == "skipped_subscription_busy"


async def test_channel_event_fire_never_skips():
    sp = await _busy_provider()
    skip = await check_subscription_busy(
        _sub(), DispatchDeps(storage_provider=sp, claim_engine=None),
        fire_context={"event": {"thread_anchor": "thr-1"}},
    )
    assert skip is None


async def test_omitted_fire_context_keeps_the_old_behaviour():
    sp = await _busy_provider()
    skip = await check_subscription_busy(
        _sub(), DispatchDeps(storage_provider=sp, claim_engine=None),
    )
    assert skip is not None
