"""Trigger fire correctness (Task 70): the skip gate reads every live session (W3), the trigger row's last_fire_error is scrubbed (W44), a
tick that passes during a fire is still fired (W47), a delayed trigger whose fire raised stays armed (W48), and an update or a token rotate
keeps a concurrent fire's bookkeeping (W49)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import primer.trigger.dispatch as dispatch_mod
from primer.int.claim import ClaimKind
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.trigger import (
    AgentFreshSubConfig,
    DelayedTriggerConfig,
    ScheduledTriggerConfig,
    SessionAppendSubConfig,
    Subscription,
    Trigger,
    WebhookTriggerConfig,
)
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger import service
from primer.trigger.row_writes import write_trigger_fields
from primer.trigger.subscribers import DispatchDeps, check_subscription_busy
from primer.worker.engine_handlers import run_engine_trigger
from tests.trigger.test_trigger_claim_release import _fired, _on_a_fake_clock

NOW = datetime.now(UTC)
TICK = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "fire.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


# ---------------------------------------------------------------------------
# W3: the parallelism skip gate
# ---------------------------------------------------------------------------


def _session(sid: str, **fields) -> WorkspaceSession:
    base = {"status": SessionStatus.RUNNING, "turn_no": 3, "created_at": NOW}
    return WorkspaceSession(
        id=sid, workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"), metadata={"subscription_id": "sb-1"}, **{**base, **fields},
    )


async def _busy(sp, *sessions: WorkspaceSession, fire_context: dict | None = None):
    store = sp.get_storage(WorkspaceSession)
    for s in sessions:
        await store.create(s)
    sub = Subscription(
        id="sb-1", trigger_id="tr-1", config=AgentFreshSubConfig(workspace_id="w1", agent_id="ag1"), parallelism="skip", created_at=NOW,
    )
    return await check_subscription_busy(sub, DispatchDeps(storage_provider=sp, claim_engine=None), fire_context=fire_context or {"kind": "scheduled"})


async def test_gate_names_the_first_live_session(sp) -> None:
    res = await _busy(sp, _session("sess-b"), _session("sess-a"), _session("sess-0", status=SessionStatus.ENDED))
    assert (res.ok, res.skipped, res.error_code, res.error_message) == (True, True, "skipped_subscription_busy", "session 'sess-a' still in-flight")


@pytest.mark.parametrize("fields", [
    {"status": SessionStatus.ENDED},
    {"status": SessionStatus.WAITING, "cancel_requested": True},
    {"status": SessionStatus.CREATED, "turn_no": 0, "created_at": NOW - timedelta(hours=2)},
])
async def test_gate_ignores_sessions_that_do_not_hold_it(sp, fields) -> None:
    assert await _busy(sp, _session("sess-a", **fields)) is None


async def test_gate_counts_a_just_created_session_and_exempts_channel_events(sp) -> None:
    fresh = _session("sess-a", status=SessionStatus.CREATED, turn_no=0)
    assert (await _busy(sp, fresh)).error_code == "skipped_subscription_busy"
    assert await _busy(sp, fire_context={"event": {"id": "e"}}) is None


async def test_gate_sees_the_live_session_after_250_ended_runs(sp) -> None:
    ended = [_session(f"sess-{i:04d}", status=SessionStatus.ENDED) for i in range(250)]
    res = await _busy(sp, *ended, _session("sess-zzzz"))
    assert res is not None and res.error_message == "session 'sess-zzzz' still in-flight"


async def test_gate_reads_every_live_session_past_one_page(sp) -> None:
    cancelled = [_session(f"sess-{i:04d}", status=SessionStatus.WAITING, cancel_requested=True) for i in range(201)]
    res = await _busy(sp, *cancelled, _session("sess-zzzz"))
    assert res is not None and res.error_message == "session 'sess-zzzz' still in-flight"

