"""ChannelRegistry.for_session + ChannelDispatcher session routing.

``for_session`` resolves the outbound adapters through
:func:`resolve_reply_binding`, so the session-ephemeral binding takes
precedence over the workspace-standing one, and a session with no binding
(and a workspace with none) routes nowhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from primer.channel.adapter import PromptEnvelope
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


def _env() -> PromptEnvelope:
    return PromptEnvelope(
        kind="ask_user",
        workspace_id="ws-1",
        session_id="s-1",
        tool_call_id="tc-1",
        prompt="please answer",
        response_schema=None,
        choices=None,
        timeout_at_iso=None,
    )


@pytest.mark.asyncio
async def test_for_session_prefers_session_scoped_binding():
    sp = _SP()
    sp.get_storage(Workspace).put(_make_workspace(channel_id="ch-ws"))
    reg = OfflineChannelRegistry(sp)
    session = _FakeSession(
        workspace_id="ws-1",
        metadata={SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-sess"}},
    )

    adapters = await reg.for_session(session)
    assert reg.requested == ["ch-sess"]
    assert adapters == [reg.adapters["ch-sess"]]


@pytest.mark.asyncio
async def test_for_session_falls_back_to_workspace_then_none():
    sp = _SP()
    sp.get_storage(Workspace).put(_make_workspace(channel_id="ch-ws"))
    reg = OfflineChannelRegistry(sp)
    session = _FakeSession(workspace_id="ws-1", metadata={})

    adapters = await reg.for_session(session)
    assert reg.requested == ["ch-ws"]
    assert adapters == [reg.adapters["ch-ws"]]

    # Clear the workspace binding -> nothing to resolve.
    sp.get_storage(Workspace).put(_make_workspace(channel_id=None))
    reg2 = OfflineChannelRegistry(sp)
    adapters = await reg2.for_session(session)
    assert adapters == []
    assert reg2.requested == []


@pytest.mark.asyncio
async def test_dispatch_prompt_with_session_uses_for_session():
    sp = _SP()
    sp.get_storage(Workspace).put(_make_workspace(channel_id="ch-ws"))
    reg = OfflineChannelRegistry(sp)
    session = _FakeSession(
        workspace_id="ws-1",
        metadata={SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-sess"}},
    )
    d = ChannelDispatcher(registry=reg)

    await d.dispatch_prompt(envelope=_env(), session=session)

    posted = reg.adapters["ch-sess"].posted
    assert len(posted) == 1
    assert posted[0].kind == "ask_user"
