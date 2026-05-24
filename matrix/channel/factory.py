"""Registry of per-platform adapter factories."""

from __future__ import annotations

from typing import Awaitable, Callable

from matrix.channel.adapter import ChannelAdapter
from matrix.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
)
from matrix.model.except_ import ConfigError


AdapterFactory = Callable[
    [ChannelProvider, Channel, object],
    Awaitable[ChannelAdapter],
]


_FACTORIES: dict[ChannelProviderType, AdapterFactory] = {}


def register_adapter_factory(
    provider_type: ChannelProviderType,
    factory: AdapterFactory,
) -> None:
    existing = _FACTORIES.get(provider_type)
    if existing is None:
        _FACTORIES[provider_type] = factory
        return
    if existing is not factory:
        raise ConfigError(
            f"adapter factory for {provider_type.value!r} already "
            "registered; refusing to overwrite"
        )


def build_adapter(
    provider_row: ChannelProvider,
    channel_row: Channel,
    inbox: object,
) -> Awaitable[ChannelAdapter]:
    factory = _FACTORIES.get(provider_row.provider)
    if factory is None:
        sub_spec_map = {
            ChannelProviderType.SLACK: "1",
            ChannelProviderType.TELEGRAM: "2",
            ChannelProviderType.DISCORD: "3",
        }
        raise ConfigError(
            f"adapter for provider {provider_row.provider.value!r} "
            "is not installed; see Spec 3."
            + sub_spec_map[provider_row.provider]
        )
    return factory(provider_row, channel_row, inbox)


def clear_factories_for_tests() -> None:
    """Test-only: drop all registrations."""
    _FACTORIES.clear()


__all__ = [
    "AdapterFactory",
    "build_adapter",
    "clear_factories_for_tests",
    "register_adapter_factory",
]
