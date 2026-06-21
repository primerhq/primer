"""Session lifecycle relay: start ack + final-result post to the reply binding.

:func:`post_session_start_ack` and :func:`post_session_final_result`
(``primer/channel/session_relay.py``) are the session-side analogues of the
chat relay helpers. They resolve the session's reply binding via
:func:`resolve_reply_binding`, post an ``inform``-kind
:class:`PromptEnvelope` through the :class:`ChannelDispatcher`, and return
whether any channel was reached. A binding marked ``quiet`` suppresses both;
a session with no binding is silent (preserves today's non-channel behavior).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from primer.channel.dispatcher import ChannelDispatcher
from primer.channel.null_adapter import NullChannelAdapter
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.channel.session_relay import (
    post_session_final_result,
    post_session_start_ack,
)
from primer.model.workspace import (
    Workspace,
    WorkspaceChannelLink,
    WorkspaceRuntimeMeta,
)


class _FakeSession:
    def __init__(self, workspace_id: str, metadata: dict[str, Any]) -> None:
        self.id = "s-relay-1"
        self.workspace_id = workspace_id
        self.metadata = metadata


class _Storage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id: str) -> Any | None:
        return self._data.get(id)

    def put(self, entity: Any) -> None:
        self._data[entity.id] = entity


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}

    def get_storage(self, cls: type) -> _Storage:
        return self._stores.setdefault(cls, _Storage())


def _make_workspace(channel_id: str | None = None) -> Workspace:
    return Workspace(
        id="ws-relay",
        template_id="t-1",
        provider_id="p-1",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://localhost:5959",
            token=SecretStr("tok"),
        ),
        reply_binding=(
            WorkspaceChannelLink(channel_id=channel_id) if channel_id else None
        ),
    )


class _Registry:
    """Stub registry borrowing the real ``for_session`` resolution path."""

    def __init__(self, storage_provider) -> None:
        self._storage_provider = storage_provider
        self.adapters: dict[str, NullChannelAdapter] = {}
        self.requested: list[str] = []

    async def get_adapter(self, channel_id: str) -> NullChannelAdapter:
        self.requested.append(channel_id)
        adapter = self.adapters.get(channel_id)
        if adapter is None:
            adapter = NullChannelAdapter()
            await adapter.initialize()
            self.adapters[channel_id] = adapter
        return adapter

    async def for_session(self, session):
        from primer.api.registries.channel_registry import ChannelRegistry

        return await ChannelRegistry.for_session(self, session)


@pytest.mark.asyncio
async def test_start_ack_posts_to_binding():
    sp = _SP()
    sp.get_storage(Workspace).put(_make_workspace())
    reg = _Registry(sp)
    d = ChannelDispatcher(registry=reg)
    session = _FakeSession(
        workspace_id="ws-relay",
        metadata={SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-sess"}},
    )

    reached = await post_session_start_ack(
        dispatcher=d, session=session, storage_provider=sp,
    )

    assert reached is True
    posted = reg.adapters["ch-sess"].posted
    assert len(posted) == 1
    assert posted[0].kind == "inform"
    assert posted[0].session_id == session.id


@pytest.mark.asyncio
async def test_final_result_posts_to_binding():
    sp = _SP()
    sp.get_storage(Workspace).put(_make_workspace())
    reg = _Registry(sp)
    d = ChannelDispatcher(registry=reg)
    session = _FakeSession(
        workspace_id="ws-relay",
        metadata={SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-sess"}},
    )

    reached = await post_session_final_result(
        dispatcher=d, session=session, storage_provider=sp, text="all done",
    )

    assert reached is True
    posted = reg.adapters["ch-sess"].posted
    assert len(posted) == 1
    assert posted[0].kind == "inform"
    assert posted[0].prompt == "all done"


@pytest.mark.asyncio
async def test_quiet_binding_suppresses_ack_and_final():
    sp = _SP()
    sp.get_storage(Workspace).put(_make_workspace())
    reg = _Registry(sp)
    d = ChannelDispatcher(registry=reg)
    session = _FakeSession(
        workspace_id="ws-relay",
        metadata={
            SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-sess", "quiet": True},
        },
    )

    ack = await post_session_start_ack(
        dispatcher=d, session=session, storage_provider=sp,
    )
    final = await post_session_final_result(
        dispatcher=d, session=session, storage_provider=sp, text="done",
    )

    assert ack is False
    assert final is False
    assert reg.requested == []
    assert reg.adapters == {}


@pytest.mark.asyncio
async def test_no_binding_is_silent():
    sp = _SP()
    sp.get_storage(Workspace).put(_make_workspace(channel_id=None))
    reg = _Registry(sp)
    d = ChannelDispatcher(registry=reg)
    session = _FakeSession(workspace_id="ws-relay", metadata={})

    ack = await post_session_start_ack(
        dispatcher=d, session=session, storage_provider=sp,
    )
    final = await post_session_final_result(
        dispatcher=d, session=session, storage_provider=sp, text="done",
    )

    assert ack is False
    assert final is False
    assert reg.requested == []
