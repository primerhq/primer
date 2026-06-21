"""Channel-event + reply-binding observability metrics.

These tests pin the four new counters declared in
:mod:`primer.observability.metrics`:

  * ``channel_events_normalized_total{event_type, provider}``
  * ``channel_events_matched_total{event_type, provider}``
  * ``channel_events_dispatched_total{event_type, provider}``
  * ``reply_binding_resolutions_total{scope}``

They assert each is a ``Counter``, increments under labels, and is exposed in
``generate_latest(registry)``. A small in-process drive of the inbound router
proves the normalized counter advances on a real event, and the reply-binding
resolver is exercised in isolation across its three winning scopes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from prometheus_client import Counter
from prometheus_client.exposition import generate_latest

from primer.channel.correlation import CorrelationStore
from primer.channel.inbound_router import ChannelInboundRouter
from primer.channel.reply_binding import (
    SESSION_REPLY_BINDING_KEY,
    resolve_reply_binding,
)
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
from primer.storage.sqlite import SqliteStorageProvider


# Always start each test with a fresh registry so counters do not accumulate.
@pytest.fixture(autouse=True)
def _reset_metrics():
    import primer.observability.metrics as m
    m.reset_for_test()
    yield
    m.reset_for_test()


class TestChannelMetricsDeclared:
    def test_normalized_is_counter(self) -> None:
        import primer.observability.metrics as m
        assert isinstance(m.channel_events_normalized_total, Counter)

    def test_matched_is_counter(self) -> None:
        import primer.observability.metrics as m
        assert isinstance(m.channel_events_matched_total, Counter)

    def test_dispatched_is_counter(self) -> None:
        import primer.observability.metrics as m
        assert isinstance(m.channel_events_dispatched_total, Counter)

    def test_reply_binding_is_counter(self) -> None:
        import primer.observability.metrics as m
        assert isinstance(m.reply_binding_resolutions_total, Counter)

    def test_normalized_increments_under_labels(self) -> None:
        import primer.observability.metrics as m
        m.channel_events_normalized_total.labels(
            event_type="message.posted", provider="slack"
        ).inc()
        val = m.channel_events_normalized_total.labels(
            event_type="message.posted", provider="slack"
        )._value.get()
        assert val == 1.0

    def test_matched_increments_under_labels(self) -> None:
        import primer.observability.metrics as m
        m.channel_events_matched_total.labels(
            event_type="command.invoked", provider="telegram"
        ).inc()
        val = m.channel_events_matched_total.labels(
            event_type="command.invoked", provider="telegram"
        )._value.get()
        assert val == 1.0

    def test_dispatched_increments_under_labels(self) -> None:
        import primer.observability.metrics as m
        m.channel_events_dispatched_total.labels(
            event_type="command.invoked", provider="discord"
        ).inc()
        val = m.channel_events_dispatched_total.labels(
            event_type="command.invoked", provider="discord"
        )._value.get()
        assert val == 1.0

    def test_reply_binding_increments_by_scope(self) -> None:
        import primer.observability.metrics as m
        m.reply_binding_resolutions_total.labels(scope="session").inc()
        val = m.reply_binding_resolutions_total.labels(scope="session")._value.get()
        assert val == 1.0

    def test_metric_names_in_generate_latest(self) -> None:
        import primer.observability.metrics as m
        # Touch each metric so a labelled series exists in the output.
        m.channel_events_normalized_total.labels(
            event_type="message.posted", provider="slack"
        ).inc()
        m.channel_events_matched_total.labels(
            event_type="command.invoked", provider="slack"
        ).inc()
        m.channel_events_dispatched_total.labels(
            event_type="command.invoked", provider="slack"
        ).inc()
        m.reply_binding_resolutions_total.labels(scope="none").inc()
        text = generate_latest(m.registry).decode()
        assert "channel_events_normalized_total" in text
        assert "channel_events_matched_total" in text
        assert "channel_events_dispatched_total" in text
        assert "reply_binding_resolutions_total" in text


async def _provider(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "metrics.sqlite"))
    await p.initialize()
    return p


async def _channel(p, channel_id="ch-1"):
    ch = Channel(
        id=channel_id,
        provider_id="cp-1",
        provider=ChannelProviderType.TELEGRAM,
        external_id="555",
        config=TelegramChannelConfig(chats={"enabled": False, "default_agent": None}),
    )
    await p.get_storage(Channel).create(ch)
    return ch


def _event(channel_id):
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM,
        provider_id="cp-1",
        event_id="ev-1",
        type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(timezone.utc),
        room_external_id="555",
        channel_id=channel_id,
        surface="channel",
        thread_anchor=None,
        sender=EventSender(external_id="u-1", display_name="Cara"),
        text="hello",
    )


@pytest.mark.asyncio
async def test_inbound_router_increments_normalized_counter(tmp_path: Path):
    import primer.observability.metrics as m

    p = await _provider(tmp_path)
    ch = await _channel(p)
    store = CorrelationStore(p)
    router = ChannelInboundRouter(storage_provider=p, correlation_store=store)

    await router.route_event(event=_event(ch.id), channel=ch)

    val = m.channel_events_normalized_total.labels(
        event_type="message.posted", provider="telegram"
    )._value.get()
    assert val == 1.0


class _StubSession:
    def __init__(self, *, metadata=None, workspace_id="ws-1"):
        self.metadata = metadata or {}
        self.workspace_id = workspace_id


class _StubStorage:
    def __init__(self, ws):
        self._ws = ws

    async def get(self, _id):
        return self._ws


class _StubProvider:
    def __init__(self, ws):
        self._ws = ws

    def get_storage(self, _model):
        return _StubStorage(self._ws)


class _StubWorkspace:
    def __init__(self, reply_binding):
        self.reply_binding = reply_binding


class _WsBinding:
    def __init__(self, channel_id):
        self.channel_id = channel_id


@pytest.mark.asyncio
async def test_reply_binding_resolution_counter_increments_by_scope():
    import primer.observability.metrics as m

    # session scope: ephemeral binding in session metadata wins.
    session = _StubSession(
        metadata={SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-sess"}}
    )
    await resolve_reply_binding(session, storage_provider=_StubProvider(None))
    assert (
        m.reply_binding_resolutions_total.labels(scope="session")._value.get()
        == 1.0
    )

    # workspace scope: no session binding, workspace has a standing reply binding.
    ws = _StubWorkspace(reply_binding=_WsBinding(channel_id="ch-ws"))
    await resolve_reply_binding(
        _StubSession(), storage_provider=_StubProvider(ws)
    )
    assert (
        m.reply_binding_resolutions_total.labels(scope="workspace")._value.get()
        == 1.0
    )

    # none scope: neither resolves.
    await resolve_reply_binding(
        _StubSession(), storage_provider=_StubProvider(_StubWorkspace(None))
    )
    assert (
        m.reply_binding_resolutions_total.labels(scope="none")._value.get()
        == 1.0
    )
