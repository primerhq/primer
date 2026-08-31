"""S6 P5 / crosscheck C4: the chat subscription kinds are gone.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 8. S1 P7 carved
these out; they die here, after the thread-mapped replacement landed.
"""

from __future__ import annotations

import importlib

import pytest
from pydantic import TypeAdapter

from primer.model.trigger import SubscriptionConfig, SubscriptionKind
from primer.trigger.subscribers import get_dispatcher


@pytest.mark.parametrize("name", ["CHAT_MESSAGE", "START_CHAT"])
def test_kind_enum_members_are_gone(name):
    assert not hasattr(SubscriptionKind, name)


@pytest.mark.parametrize("kind", ["chat_message", "start_chat"])
def test_config_no_longer_validates(kind):
    ta = TypeAdapter(SubscriptionConfig)
    with pytest.raises(Exception):
        ta.validate_python({"kind": kind, "chat_id": "c1", "agent_id": "a1"})


@pytest.mark.parametrize("kind", ["chat_message", "start_chat"])
def test_no_dispatcher_is_registered(kind):
    with pytest.raises(KeyError):
        get_dispatcher(kind)


@pytest.mark.parametrize("module", [
    "primer.channel.chat_dispatcher",
    "primer.channel.chat_inbox",
    "primer.channel.chat_router",
    "primer.channel.commands",
    "primer.trigger.subscribers.chat_message",
    "primer.trigger.subscribers.start_chat",
    # [CROSSPLAN 2026-08-16, F30, F47] the CHAT claim lane goes with them
    "primer.claim.adapters.chats",
])
def test_deleted_modules_are_unimportable(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_the_chat_claim_lane_is_gone():
    """[CROSSPLAN 2026-08-16, F30, F47]

    The lane's only reason to exist was the relay this task deletes, and
    ``run_engine_chat`` imports ``primer.channel.chat_dispatcher``, which
    stops existing in this same commit.
    """
    from primer.int.claim import ClaimKind
    from primer.worker import engine_handlers

    assert not hasattr(ClaimKind, "CHAT")
    assert not hasattr(engine_handlers, "run_engine_chat")
