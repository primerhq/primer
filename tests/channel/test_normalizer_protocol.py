from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.channel.normalizer import ChannelEventNormalizer, ProviderCapabilities
from primer.model.channel import ChannelProviderType
from primer.model.channel_event import ChannelEvent, EventSender, NormalizedEventType


def test_provider_capabilities_shape():
    caps = ProviderCapabilities(
        provider=ChannelProviderType.SLACK,
        supported={NormalizedEventType.MESSAGE_POSTED},
        prerequisites={"event_subscriptions": "subscribe to message.channels"},
    )
    assert caps.supported == {NormalizedEventType.MESSAGE_POSTED}
    assert isinstance(caps.prerequisites["event_subscriptions"], str)
    assert caps.prerequisites["event_subscriptions"]


class _FakeNormalizer:
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=ChannelProviderType.SLACK,
            supported={NormalizedEventType.MESSAGE_POSTED},
        )

    async def normalize(self, raw) -> ChannelEvent | None:
        if raw == {"kind": "msg"}:
            return ChannelEvent(
                provider=ChannelProviderType.SLACK,
                provider_id="p",
                event_id="e1",
                type=NormalizedEventType.MESSAGE_POSTED,
                occurred_at=datetime.now(timezone.utc),
                surface="channel",
                sender=EventSender(external_id="U1"),
            )
        return None


@pytest.mark.asyncio
async def test_in_memory_fake_satisfies_protocol():
    assert isinstance(_FakeNormalizer(), ChannelEventNormalizer) is True
    assert isinstance(await _FakeNormalizer().normalize({"kind": "msg"}), ChannelEvent)
    assert await _FakeNormalizer().normalize({"kind": "other"}) is None
