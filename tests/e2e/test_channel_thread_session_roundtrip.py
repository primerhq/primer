"""E2E: a platform thread IS a session (S6 section 5).

Replaces test_channel_regression_message_to_chat.py: the default inbound
path no longer opens a chat, it creates and maps a session. Runs in process
on a real SqliteStorageProvider; no HTTP, no LLM, no Postgres. The e2e
conftest's PRIMER_RUN_E2E gate collect-ignores the module by default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import primer.channel.event_dispatch as ed
from primer.channel.correlation import CorrelationStore
from primer.channel.event_dispatch import ChannelEventRouter
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.model.channel import (
    Channel,
    ChannelProviderType,
    TelegramChannelConfig,
)
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.trigger import (
    AgentFreshSubConfig,
    ChannelTriggerConfig,
    Subscription,
    Trigger,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.steer_delivery import SteerDelivery
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger.dispatch import FireResult
from primer.trigger.subscribers import DispatchDeps


def _event(anchor: str, text: str) -> ChannelEvent:
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM, provider_id="cp-rt",
        event_id=f"ev-{anchor}-{text}",
        type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(UTC), channel_id="ch-rt", surface="thread",
        thread_anchor=anchor, sender=EventSender(external_id="u1"), text=text,
    )


@pytest.mark.asyncio
async def test_thread_roundtrip(tmp_path: Path, monkeypatch) -> None:
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "s6rt.sqlite"))
    await p.initialize()

    channel = Channel(
        id="ch-rt", provider_id="cp-rt",
        provider=ChannelProviderType.TELEGRAM, external_id="777",
        config=TelegramChannelConfig(),
    )
    await p.get_storage(Channel).create(channel)
    trigger = Trigger(
        id="tr-rt", slug="rt-channel", name="RT",
        config=ChannelTriggerConfig(provider_id="cp-rt", channel_id="ch-rt"),
        created_at=datetime.now(UTC),
    )
    await p.get_storage(Trigger).create(trigger)
    await p.get_storage(Subscription).create(Subscription(
        id="sb-rt", trigger_id="tr-rt",
        config=AgentFreshSubConfig(workspace_id="w-rt", agent_id="ag-rt"),
        created_at=datetime.now(UTC),
    ))

    created: list[str] = []

    async def _fake_fire(*, trigger_id, scheduled_for, deps, extra_context=None):
        sid = f"s-rt-{len(created)}"
        created.append(sid)
        await p.get_storage(WorkspaceSession).create(WorkspaceSession(
            id=sid, workspace_id="w-rt",
            binding=AgentSessionBinding(agent_id="ag-rt"),
            status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
        ))
        return FireResult(
            fire_id=f"fire-{sid}",
            results=[{"ok": True, "skipped": False, "artefact_id": sid}],
        )

    steers: list[tuple[str, str]] = []

    async def _fake_deliver(**kw):
        steers.append((kw["session_id"], kw["text"]))
        return SteerDelivery(outcome="queued", session_id=kw["session_id"])

    monkeypatch.setattr(ed, "deliver_steer", _fake_deliver)

    def _router():
        return ChannelEventRouter(
            storage_provider=p,
            correlation_store=CorrelationStore(p),
            fire_deps=DispatchDeps(storage_provider=p, claim_engine=None),
            event_bus=None,
            fire_trigger=_fake_fire,
        )

    # 1. New thread -> one session, mapped, reply-bound to the thread.
    first = await _router().route_event(
        event=_event("thr-a", "hello"), channel=channel,
    )
    assert first.kind == "fired"
    assert first.session_id == "s-rt-0"
    record = await CorrelationStore(p).lookup("ch-rt", "thr-a")
    assert record.session_id == "s-rt-0"
    row = await p.get_storage(WorkspaceSession).get("s-rt-0")
    assert row.metadata[SESSION_REPLY_BINDING_KEY]["anchor"] == "thr-a"

    # 2. Reply in the same thread -> same session, no second session.
    second = await _router().route_event(
        event=_event("thr-a", "any update?"), channel=channel,
    )
    assert second.kind == "steer"
    assert second.session_id == "s-rt-0"
    assert steers == [("s-rt-0", "any update?")]
    assert len(created) == 1

    # 3. Session deleted, thread lives on -> next reply makes a fresh one.
    await p.get_storage(WorkspaceSession).delete("s-rt-0")

    async def _missing_deliver(**kw):
        return SteerDelivery(outcome="missing")

    monkeypatch.setattr(ed, "deliver_steer", _missing_deliver)
    third = await _router().route_event(
        event=_event("thr-a", "still there?"), channel=channel,
    )
    assert third.kind == "fired"
    assert third.session_id == "s-rt-1"
    remapped = await CorrelationStore(p).lookup("ch-rt", "thr-a")
    assert remapped.session_id == "s-rt-1"

    sessions = (await p.get_storage(WorkspaceSession).find(
        None, OffsetPage(offset=0, length=50),
    )).items
    assert [s.id for s in sessions] == ["s-rt-1"]
