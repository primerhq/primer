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


# ---------------------------------------------------------------------------
# W44: the trigger row's last_fire_error
# ---------------------------------------------------------------------------


async def _fire_raising(storage, monkeypatch, message: str) -> Trigger:
    await storage.get_storage(Trigger).create(Trigger(
        id="tr-1", slug="tr-x", name="x", config=DelayedTriggerConfig(fire_at=NOW), enabled=True, next_fire_at=NOW, created_at=NOW,
    ))
    await storage.get_storage(Subscription).create(Subscription(
        owner=PrincipalRef.system(), id="sb-1", trigger_id="tr-1", config=SessionAppendSubConfig(session_id="cn-1"),
        payload_template="hi", enabled=True, created_at=NOW,
    ))

    class _Raises:
        async def dispatch(self, *args, **kwargs):
            raise RuntimeError(message)

    monkeypatch.setattr(dispatch_mod, "get_dispatcher", lambda kind: _Raises())
    await dispatch_mod.fire_trigger(trigger_id="tr-1", scheduled_for=None, deps=DispatchDeps(storage_provider=storage, claim_engine=None))
    return await storage.get_storage(Trigger).get("tr-1")


async def test_last_fire_error_shape_is_unchanged(fake_storage_provider, monkeypatch) -> None:
    row = await _fire_raising(fake_storage_provider, monkeypatch, "boom")
    assert row.last_fire_error == json.dumps({"code": "dispatch_failed", "subscription_id": "sb-1", "message": "boom"})


async def test_last_fire_error_is_scrubbed_on_the_trigger_row(fake_storage_provider, monkeypatch) -> None:
    row = await _fire_raising(
        fake_storage_provider, monkeypatch, "connect failed: https://user:hunter2@internal.example/hook?token=abc Bearer sk-live-123",
    )
    message = json.loads(row.last_fire_error)["message"]
    assert message == "connect failed: <url> Bearer <redacted>"


# ---------------------------------------------------------------------------
# W47: a tick that passes during the fire
# ---------------------------------------------------------------------------


def _slow_fire(monkeypatch, clock: dict, *, takes: timedelta) -> None:
    real = dispatch_mod.fire_trigger
    calls = {"n": 0}

    async def _fire(**kwargs):
        result = await real(**kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            clock["now"] += takes
        return result

    monkeypatch.setattr(dispatch_mod, "fire_trigger", _fire)


async def test_a_fire_that_ends_before_the_next_tick_advances_to_it(sp, monkeypatch) -> None:
    clock, engine, pool = _on_a_fake_clock(monkeypatch, sp, TICK - timedelta(seconds=30))
    monkeypatch.setattr(service, "_now", lambda: clock["now"])  # the create's next_fire_at too
    made = await service.create_trigger(
        slug="minutely", name="m", description=None, config=ScheduledTriggerConfig(cron="* * * * *", timezone="UTC", catchup="all"),
        deps=service.ServiceDeps(storage_provider=sp, claim_engine=engine),
    )
    _slow_fire(monkeypatch, clock, takes=timedelta(seconds=20))
    clock["now"] = TICK + timedelta(seconds=2)
    [lease] = await engine.claim_due("w", max_count=5)
    await run_engine_trigger(pool, lease)  # type: ignore[arg-type]
    assert (await sp.get_storage(Trigger).get(made.id)).next_fire_at == TICK + timedelta(minutes=1)
    assert await engine.claim_due("w", max_count=5) == []


@pytest.mark.parametrize(("catchup", "takes", "then_fired"), [
    ("all", timedelta(seconds=70), [TICK + timedelta(minutes=1)]),
    ("all", timedelta(seconds=130), [TICK + timedelta(minutes=1), TICK + timedelta(minutes=2)]),
    ("none", timedelta(seconds=130), [TICK + timedelta(minutes=2)]),
])
async def test_a_tick_that_passes_during_the_fire_is_fired_by_the_next_claim(sp, monkeypatch, catchup, takes, then_fired) -> None:
    clock, engine, pool = _on_a_fake_clock(monkeypatch, sp, TICK - timedelta(seconds=30))
    monkeypatch.setattr(service, "_now", lambda: clock["now"])  # the create's next_fire_at too
    made = await service.create_trigger(
        slug="minutely", name="m", description=None, config=ScheduledTriggerConfig(cron="* * * * *", timezone="UTC", catchup=catchup),
        deps=service.ServiceDeps(storage_provider=sp, claim_engine=engine),
    )
    _slow_fire(monkeypatch, clock, takes=takes)
    clock["now"] = TICK + timedelta(seconds=2)
    [lease] = await engine.claim_due("w", max_count=5)
    await run_engine_trigger(pool, lease)  # type: ignore[arg-type]
    assert (await sp.get_storage(Trigger).get(made.id)).next_fire_at == TICK + timedelta(minutes=1)

    clock["now"] += timedelta(seconds=1)
    [lease] = await engine.claim_due("w", max_count=5)
    await run_engine_trigger(pool, lease)  # type: ignore[arg-type]
    assert await _fired(sp) == [t.isoformat() for t in [TICK, *then_fired]]


# ---------------------------------------------------------------------------
# W48: a delayed trigger whose fire raised
# ---------------------------------------------------------------------------


async def _delayed(sp, monkeypatch):
    clock, engine, pool = _on_a_fake_clock(monkeypatch, sp, TICK - timedelta(hours=1))
    pool.config = SimpleNamespace(base_backoff_seconds=2.0, max_backoff_seconds=300.0)
    made = await service.create_trigger(
        slug="once", name="o", description=None, config=DelayedTriggerConfig(fire_at=TICK),
        deps=service.ServiceDeps(storage_provider=sp, claim_engine=engine),
    )
    return clock, engine, pool, made


async def test_a_delayed_fire_with_a_failed_delivery_still_disables_the_trigger(sp, monkeypatch) -> None:
    clock, engine, pool, made = await _delayed(sp, monkeypatch)
    real = dispatch_mod.fire_trigger

    async def _fire(**kwargs):
        result = await real(**kwargs)
        result.results.append({"subscription_id": "sb-x", "ok": False, "error_code": "dispatch_failed"})
        return result

    monkeypatch.setattr(dispatch_mod, "fire_trigger", _fire)
    clock["now"] = TICK + timedelta(seconds=1)
    [lease] = await engine.claim_due("w", max_count=5)
    await run_engine_trigger(pool, lease)  # type: ignore[arg-type]
    row = await sp.get_storage(Trigger).get(made.id)
    assert (row.enabled, row.next_fire_at) == (False, None)
    assert not await engine.has_lease(ClaimKind.TRIGGER, made.id)


async def test_a_delayed_fire_that_raised_before_dispatch_stays_armed_and_fires_later(sp, monkeypatch) -> None:
    clock, engine, pool, made = await _delayed(sp, monkeypatch)
    real = dispatch_mod.fire_trigger
    calls = {"n": 0}

    async def _fire(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("storage hiccup")
        return await real(**kwargs)

    monkeypatch.setattr(dispatch_mod, "fire_trigger", _fire)
    clock["now"] = TICK + timedelta(seconds=1)
    [lease] = await engine.claim_due("w", max_count=5)
    await run_engine_trigger(pool, lease)  # type: ignore[arg-type]
    row = await sp.get_storage(Trigger).get(made.id)
    assert (row.enabled, row.next_fire_at) == (True, TICK)
    assert await engine.has_lease(ClaimKind.TRIGGER, made.id)
    assert await engine.claim_due("w", max_count=5) == [], "requeued with a backoff, not at once"

    clock["now"] += timedelta(minutes=10)
    [lease] = await engine.claim_due("w", max_count=5)
    await run_engine_trigger(pool, lease)  # type: ignore[arg-type]
    assert await _fired(sp) == [TICK.isoformat()]
    row = await sp.get_storage(Trigger).get(made.id)
    assert (row.enabled, row.next_fire_at) == (False, None)
    assert not await engine.has_lease(ClaimKind.TRIGGER, made.id)

