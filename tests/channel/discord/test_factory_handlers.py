"""Discord factory handler-registration tests."""

from __future__ import annotations

import pytest

discord = pytest.importorskip("discord")

from primer.channel.discord.factory import _install_handlers


class _RecordingClient:
    def __init__(self) -> None:
        self.listeners: list[str] = []

    def add_listener(self, func, name):
        self.listeners.append(name)

    def event(self, coro):  # must NOT be used (keys off __name__)
        self.listeners.append("EVENT:" + coro.__name__)
        return coro


def test_handlers_register_under_real_gateway_event_names():
    # Regression: the interaction/message handlers are named _on_interaction /
    # _on_message, so client.event() would bind them to the wrong attribute and
    # they'd never dispatch. They must be registered as on_interaction /
    # on_message via add_listener.
    client = _RecordingClient()
    _install_handlers("dc-prov-handler-test", client)
    assert "on_interaction" in client.listeners
    assert "on_message" in client.listeners
    assert not any(name.startswith("EVENT:") for name in client.listeners)
