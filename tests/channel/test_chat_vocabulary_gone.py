"""S6 P5: no chat vocabulary survives in channel/.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 7.
"""

from __future__ import annotations

import importlib

import pytest

from primer.channel.correlation import CorrelationStore
from primer.model.channel import ChatConfig
from primer.model.channel_correlation import ChannelCorrelation


@pytest.mark.parametrize("attr", ["upsert_chat", "set_active_chat"])
def test_chat_writers_are_gone(attr):
    assert not hasattr(CorrelationStore, attr)


def test_active_chat_anchor_is_gone():
    module = importlib.import_module("primer.channel.correlation")
    assert not hasattr(module, "ACTIVE_CHAT_ANCHOR")


def test_correlation_has_no_chat_id():
    assert "chat_id" not in ChannelCorrelation.model_fields


def test_correlation_kind_is_session_only():
    with pytest.raises(Exception):
        ChannelCorrelation(channel_id="ch-1", anchor="a", kind="chat")


@pytest.mark.parametrize(
    "field", ["default_agent", "allow_agent_switch", "allowed_agents"],
)
def test_chat_config_agent_fields_are_gone(field):
    assert field not in ChatConfig.model_fields


def test_constraints_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("primer.channel.constraints")
