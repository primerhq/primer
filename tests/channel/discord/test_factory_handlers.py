"""Discord factory handler-registration tests."""

from __future__ import annotations

import asyncio

import pytest

discord = pytest.importorskip("discord")

from primer.channel.discord.factory import _install_handlers


class _FakeClient:
    """Bare object that accepts attribute assignment, like discord.Client."""


def test_handlers_bound_to_real_gateway_event_names():
    # Regression: the base discord.Client dispatches by looking up
    # self.on_<event>; it has no add_listener, and client.event would bind the
    # _on_interaction/_on_message coroutines under the wrong attribute. The
    # handlers must land on on_interaction / on_message so clicks and thread
    # replies actually dispatch.
    client = _FakeClient()
    _install_handlers("dc-prov-handler-test", client)
    assert asyncio.iscoroutinefunction(getattr(client, "on_interaction", None))
    assert asyncio.iscoroutinefunction(getattr(client, "on_message", None))
