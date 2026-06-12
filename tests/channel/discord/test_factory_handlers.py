"""Discord factory handler-registration tests."""

from __future__ import annotations

import asyncio

import pytest

discord = pytest.importorskip("discord")

from pydantic import SecretStr

from primer.channel.discord.factory import _install_handlers
from primer.model.channel import (
    Channel, ChannelProviderType, DiscordChannelProviderConfig,  # noqa: F401
)


class _FakeClient:
    """Bare object that accepts attribute assignment, like discord.Client."""

    def is_ready(self) -> bool:
        # Not yet ready: the already-ready sync path is exercised separately.
        return False


def _channel() -> Channel:
    return Channel(id="ch-1", provider_id="cp-1", external_id="9001")


def test_handlers_bound_to_real_gateway_event_names():
    # Regression: the base discord.Client dispatches by looking up
    # self.on_<event>; it has no add_listener, and client.event would bind the
    # _on_interaction/_on_message coroutines under the wrong attribute. The
    # handlers must land on on_interaction / on_message so clicks and thread
    # replies actually dispatch.
    client = _FakeClient()
    _install_handlers("dc-prov-handler-test", client, _channel())
    assert asyncio.iscoroutinefunction(getattr(client, "on_interaction", None))
    assert asyncio.iscoroutinefunction(getattr(client, "on_message", None))
