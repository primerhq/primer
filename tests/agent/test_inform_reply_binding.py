"""SessionInformSink routes inform posts through the session reply binding."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from primer.agent.inform import SessionInformSink
from primer.channel.dispatcher import ChannelDispatcher
from primer.channel.null_adapter import NullChannelAdapter
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.model.workspace import (
    Workspace,
    WorkspaceChannelLink,
    WorkspaceRuntimeMeta,
)


class _FakeSession:
    def __init__(self, workspace_id: str, metadata: dict[str, Any]) -> None:
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


class _Registry:
    def __init__(self, storage_provider) -> None:
        self._storage_provider = storage_provider
        self.adapters: dict[str, NullChannelAdapter] = {}

    async def get_adapter(self, channel_id: str) -> NullChannelAdapter:
        adapter = self.adapters.get(channel_id)
        if adapter is None:
            adapter = NullChannelAdapter()
            await adapter.initialize()
            self.adapters[channel_id] = adapter
        return adapter

    async def for_session(self, session):
        from primer.api.registries.channel_registry import ChannelRegistry

        return await ChannelRegistry.for_session(self, session)

    async def for_workspace(self, workspace_id):
        from primer.api.registries.channel_registry import ChannelRegistry

        return await ChannelRegistry.for_workspace(self, workspace_id)


def _make_workspace(channel_id: str | None = None) -> Workspace:
    return Workspace(
        id="ws-1",
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


@pytest.mark.asyncio
async def test_inform_routes_to_session_binding():
    sp = _SP()
    # Workspace-standing binding points elsewhere; the session-scoped binding
    # must win, proving the sink threads the session through the dispatcher.
    sp.get_storage(Workspace).put(_make_workspace(channel_id="ch-ws"))
    reg = _Registry(sp)
    dispatcher = ChannelDispatcher(registry=reg)

    session = _FakeSession(
        workspace_id="ws-1",
        metadata={SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-sess"}},
    )
    sink = SessionInformSink(
        dispatcher=dispatcher,
        workspace_id="ws-1",
        session_id="s-1",
        session=session,
    )

    n = await sink("hi")
    assert n == 1
    assert "ch-ws" not in reg.adapters
    posted = reg.adapters["ch-sess"].posted
    assert len(posted) == 1
    assert posted[0].kind == "inform"
    assert posted[0].prompt == "hi"
