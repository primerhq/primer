"""SessionInformSink routes inform posts through the session reply binding."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from primer.agent.inform import SessionInformSink
from primer.channel.dispatcher import ChannelDispatcher
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.model.workspace import (
    Workspace,
    WorkspaceChannelLink,
    WorkspaceRuntimeMeta,
)
from tests._support.channel_offline import OfflineChannelRegistry


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
    async def get_system_state(self):
        from primer.model.system_state import SystemState

        return SystemState()

    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}

    def get_storage(self, cls: type) -> _Storage:
        return self._stores.setdefault(cls, _Storage())


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
    reg = OfflineChannelRegistry(sp)
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
