"""Single-vs-multi capability map."""

from __future__ import annotations

import pytest

from primer.channel.adapter import provider_supports_threads
from primer.model.channel import ChannelProviderType


@pytest.mark.parametrize("ptype,expected", [
    (ChannelProviderType.SLACK, True),
    (ChannelProviderType.DISCORD, True),
    (ChannelProviderType.TELEGRAM, False),
])
def test_supports_threads(ptype, expected):
    assert provider_supports_threads(ptype) is expected
