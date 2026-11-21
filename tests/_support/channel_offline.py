"""Offline dependencies for channel adapter and routing tests."""

from primer.api.registries.channel_registry import ChannelRegistry
from primer.channel.adapter import ChannelAdapter
from primer.channel.null_adapter import NullChannelAdapter
from primer.model.channel import Channel, ChannelProvider


def stub_connection(monkeypatch, pool, client) -> None:
    """Serve one acquired SDK client and check its identity on release."""
    acquired_provider = None

    async def acquire(provider):
        nonlocal acquired_provider
        assert acquired_provider is None
        acquired_provider = provider
        return client

    async def release(provider, *, expected):
        nonlocal acquired_provider
        assert provider is acquired_provider
        assert expected is client
        acquired_provider = None

    monkeypatch.setattr(pool, "acquire", acquire)
    monkeypatch.setattr(pool, "release", release)


class OfflineChannelRegistry(ChannelRegistry):
    """Use real binding resolution with recorded lookups and offline adapters."""

    def __init__(self, storage_provider, adapters: dict[str, ChannelAdapter] | None = None) -> None:
        super().__init__(
            storage_provider=storage_provider,
            channel_storage=storage_provider.get_storage(Channel),
            channel_provider_storage=storage_provider.get_storage(ChannelProvider),
            inbox=None,
        )
        self.adapters = adapters if adapters is not None else {}
        self.requested: list[str] = []

    async def get_adapter(self, channel_id: str) -> ChannelAdapter:
        self.requested.append(channel_id)
        if channel_id not in self.adapters:
            adapter = NullChannelAdapter()
            await adapter.initialize()
            self.adapters[channel_id] = adapter
        return self.adapters[channel_id]
