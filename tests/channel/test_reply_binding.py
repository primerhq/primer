"""Tests for the unified reply-binding model and resolver."""

from __future__ import annotations

from typing import Any

import pytest

from primer.channel.reply_binding import (
    ReplyBinding,
    ReplyTarget,
    SESSION_REPLY_BINDING_KEY,
    resolve_reply_binding,
)
from primer.model.workspace import (
    Workspace,
    WorkspaceChannelLink,
    WorkspaceRuntimeMeta,
)


def test_reply_binding_model_defaults_and_roundtrip():
    assert ReplyBinding(channel_id="ch-1").anchor is None
    assert ReplyBinding(channel_id="ch-1").quiet is False
    assert ReplyBinding(channel_id="ch-1").shared_anchor is False
    dumped = ReplyBinding(channel_id="ch-1", anchor="ts-9").model_dump()
    # shared_anchor is round 2 of #932 (finding 1): True for the workspace's standing binding and an explicit reply_target, whose
    # thread several sessions post into; False for a session-owned binding.
    assert dumped == {"channel_id": "ch-1", "anchor": "ts-9", "quiet": False, "silent": False, "shared_anchor": False}
    assert ReplyBinding.model_validate(dumped) == ReplyBinding(
        channel_id="ch-1", anchor="ts-9"
    )


# ===========================================================================
# resolve_reply_binding precedence
# ===========================================================================


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
    from datetime import datetime, timezone

    from pydantic import SecretStr

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
async def test_resolve_precedence_session_over_workspace_over_none():
    sp = _SP()
    sp.get_storage(Workspace).put(_make_workspace(channel_id="ch-ws"))

    session = _FakeSession(
        workspace_id="ws-1",
        metadata={
            SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-sess", "anchor": "ts-1"}
        },
    )

    # Session-scoped binding wins.
    resolved = await resolve_reply_binding(session, storage_provider=sp)
    assert resolved == ReplyBinding(channel_id="ch-sess", anchor="ts-1")

    # Clear the session metadata -> fall back to the workspace-standing binding, whose anchor is a SHARED one (round 2 of #932,
    # finding 1): several sessions post into it, so an ask_user question opens the session's own thread.
    session.metadata = {}
    resolved = await resolve_reply_binding(session, storage_provider=sp)
    assert resolved == ReplyBinding(channel_id="ch-ws", anchor=None, shared_anchor=True)

    # Clear the workspace binding -> None.
    sp.get_storage(Workspace).put(_make_workspace(channel_id=None))
    resolved = await resolve_reply_binding(session, storage_provider=sp)
    assert resolved is None
