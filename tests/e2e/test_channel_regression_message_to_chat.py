"""E2E regression: the default "new message -> chat" path still works.

Spec Section 14.2: in a chat-enabled channel the default behaviour for a
top-level message (no command, no rule row present) is the
correlation-first path - open or continue the thread's :class:`Chat`. The
new event-to-action mapping (Part B) must NOT regress that default: with
NO ``kind="channel"`` trigger seeded, a top-level message opens exactly
one chat, and a follow-up in the same thread appends a turn rather than
spawning a second chat.

This is the explicit regression guard the plan's DoD track 7 requires. It
runs in process on a real :class:`SqliteStorageProvider` against the real
:class:`~primer.channel.inbound_router.ChannelInboundRouter` correlation
path - no HTTP, no LLM, no Postgres. The e2e conftest's ``PRIMER_RUN_E2E``
gate collect-ignores the module by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.channel.correlation import CorrelationStore
from primer.channel.inbound_router import ChannelInboundRouter
from primer.model.agent import Agent, AgentModel
from primer.model.channel import (
    Channel,
    ChannelProviderType,
    TelegramChannelConfig,
)
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import SqliteConfig
from primer.model.storage import Op, OffsetPage
from primer.model.trigger import Subscription, Trigger
from primer.storage.q import Q
from primer.storage.sqlite import SqliteStorageProvider


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


class _FakeClaimEngine:
    async def upsert(self, kind, entity_id, *, priority=100, next_attempt_at=None):
        return None


async def _provider(tmp_path: Path) -> SqliteStorageProvider:
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "m2c.sqlite"))
    await p.initialize()
    return p


@pytest.mark.asyncio
async def test_message_to_chat_default_path_unaffected_by_rules(
    tmp_path: Path,
) -> None:
    """A top-level message in a chat-enabled channel, with NO rule row,
    opens one chat via the correlation-first path; a follow-up in the
    same thread appends a turn instead of creating a second chat.
    """
    p = await _provider(tmp_path)

    await p.get_storage(Agent).create(Agent(
        id="ag-m2c", description="default-chat agent",
        model=AgentModel(provider_id="prov", model_name="model"),
    ))
    ch = Channel(
        id="ch-m2c",
        provider_id="cp-m2c",
        provider=ChannelProviderType.TELEGRAM,
        external_id="777",
        # Chats ENABLED with a default agent: the default message-to-chat path.
        config=TelegramChannelConfig(
            chats={"enabled": True, "default_agent": "ag-m2c"},
        ),
    )
    await p.get_storage(Channel).create(ch)

    # Spec 14.2: the default is correlation-only. Prove no rule row exists.
    triggers = (await p.get_storage(Trigger).find(
        None, OffsetPage(offset=0, length=50),
    )).items
    subs = (await p.get_storage(Subscription).find(
        None, OffsetPage(offset=0, length=50),
    )).items
    assert triggers == [] and subs == [], (
        "regression precondition: no channel rule must be seeded"
    )

    store = CorrelationStore(p)
    bus = _RecordingBus()
    router = ChannelInboundRouter(
        p, store, event_bus=bus, claim_engine=_FakeClaimEngine(),
    )

    # ----- top-level message -> exactly one bound chat is opened --------
    await router.route(
        channel=ch,
        anchor=None,            # thread channel, top-level message
        reply_to="thr-m2c",     # the new thread's anchor
        is_thread_channel=True,
        sender="Cara",
        text="hello there",
    )

    chats = (await p.get_storage(Chat).find(
        None, OffsetPage(offset=0, length=50),
    )).items
    assert len(chats) == 1, f"expected one chat after first message; got {len(chats)}"
    chat = chats[0]
    assert chat.channel_binding is not None
    assert chat.channel_binding.channel_id == ch.id
    assert chat.channel_binding.thread_external_id == "thr-m2c"

    # ----- follow-up in the same thread -> appends, no second chat ------
    await router.route(
        channel=ch,
        anchor="thr-m2c",       # existing thread anchor
        reply_to=None,
        is_thread_channel=True,
        sender="Cara",
        text="any update?",
    )

    chats_after = (await p.get_storage(Chat).find(
        None, OffsetPage(offset=0, length=50),
    )).items
    assert len(chats_after) == 1, (
        "a follow-up in the same thread must reuse the chat, not create a "
        f"second one; got {len(chats_after)} chats"
    )
    assert chats_after[0].id == chat.id

    user_msgs = [
        m for m in (await p.get_storage(ChatMessage).find(
            Q(ChatMessage).where_op("chat_id", Op.EQ, chat.id).build(),
            OffsetPage(offset=0, length=50),
        )).items
        if m.kind == "user_message"
    ]
    assert len(user_msgs) == 2, (
        f"expected two appended user_messages on the one chat; got {user_msgs}"
    )
