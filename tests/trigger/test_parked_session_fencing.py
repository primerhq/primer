"""S6 P1 / crosscheck M15: S6 adds no park logic; pin what S1 guarantees.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 3 (parked_session
keeper). The wake runs against S1's epoch-fenced parked state: when the
abandon chokepoint clears parked_status/parked_state, or the park moved on,
the subscription must skip AND delete itself rather than wake a session that
is no longer waiting.
"""

from __future__ import annotations

from datetime import UTC, datetime

from primer.model.trigger import ParkedSessionSubConfig, Subscription
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.trigger.subscribers import DispatchDeps
from primer.trigger.subscribers.parked_session import ParkedSessionDispatcher
from tests.conftest import _FakeStorageProvider


def _sub() -> Subscription:
    return Subscription(
        id="sb-park",
        trigger_id="tr-park",
        config=ParkedSessionSubConfig(
            session_id="s-park",
            tool_call_id="tc-1",
            parked_at=datetime.now(UTC),
        ),
        created_at=datetime.now(UTC),
    )


async def _seed(sp, **overrides) -> None:
    row = {
        "id": "s-park",
        "workspace_id": "w1",
        "binding": AgentSessionBinding(agent_id="ag1"),
        "status": SessionStatus.RUNNING,
        "created_at": datetime.now(UTC),
        "parked_status": "parked",
        "parked_state": {"tool_call_id": "tc-1", "binding_epoch": 0},
        "binding_epoch": 0,
    }
    row.update(overrides)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(**row))


async def _fire(sp) -> tuple:
    sub = _sub()
    await sp.get_storage(Subscription).create(sub)
    result = await ParkedSessionDispatcher().dispatch(
        sub,
        rendered_payload="",
        fire_context={"fire_id": "fire-park"},
        fire_id="fire-park",
        deps=DispatchDeps(
            storage_provider=sp, claim_engine=None, event_bus=None,
        ),
    )
    return result, await sp.get_storage(Subscription).get("sb-park")


async def test_abandoned_park_skips_and_self_deletes():
    """S1's abandon chokepoint clears parked_status/parked_state."""
    sp = _FakeStorageProvider()
    await _seed(sp, parked_status=None, parked_state=None)
    result, row = await _fire(sp)
    assert result.ok is True
    assert result.skipped is True
    assert result.error_code == "skipped_session_unparked"
    assert row is None, "a stale parked_session subscription must delete itself"


async def test_park_that_moved_on_skips_and_self_deletes():
    sp = _FakeStorageProvider()
    await _seed(sp, parked_state={"tool_call_id": "tc-other", "binding_epoch": 1})
    result, row = await _fire(sp)
    assert result.ok is True
    assert result.skipped is True
    assert result.error_code == "skipped_session_unparked"
    assert row is None


async def test_ended_session_skips_and_self_deletes():
    sp = _FakeStorageProvider()
    await _seed(sp, status=SessionStatus.ENDED, parked_status=None)
    result, row = await _fire(sp)
    assert result.ok is True
    assert result.skipped is True
    assert result.error_code == "skipped_session_unparked"
    assert row is None
