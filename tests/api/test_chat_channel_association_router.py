"""Chat-channel-association CRUD + single/multi constraint enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    ChatChannelAssociation, SlackChannelProviderConfig,
    TelegramChannelProviderConfig, WorkspaceChannelAssociation,
)
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


async def _seed_provider(p, *, ptype, cp_id, ch_id):
    cp = p.get_storage(ChannelProvider)
    ch = p.get_storage(Channel)
    if ptype == ChannelProviderType.SLACK:
        cfg = SlackChannelProviderConfig(
            app_token=SecretStr("xapp-t"), bot_token=SecretStr("xoxb-t"))
    else:
        cfg = TelegramChannelProviderConfig(bot_token=SecretStr("123456:ABCDEFGHIJKLMNOP"))
    await cp.create(ChannelProvider(id=cp_id, provider=ptype, config=cfg))
    await ch.create(Channel(id=ch_id, provider_id=cp_id, external_id="X1"))


class _FakeRequest:
    """Minimal request stub: get_storage_provider(request) reads .app.state."""

    def __init__(self, sp):
        class _State:
            pass
        class _App:
            pass
        self.app = _App()
        self.app.state = _State()
        self.app.state.storage_provider = sp
        # get_storage_provider asserts both attrs are present on app.state.
        self.app.state.provider_registry = object()


@pytest.mark.asyncio
async def test_single_type_chat_then_workspace_conflicts(tmp_path: Path):
    from primer.api.routers.channels import (
        _association_on_pre_create, _chat_association_on_pre_create,
    )
    from primer.model.except_ import ConflictError

    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await _seed_provider(
        p, ptype=ChannelProviderType.TELEGRAM, cp_id="cp-1", ch_id="ch-1")

    req = _FakeRequest(p)
    cca = ChatChannelAssociation(channel_id="ch-1", default_agent_id="agent-x")
    await _chat_association_on_pre_create(cca, req)  # ok, channel empty
    await p.get_storage(ChatChannelAssociation).create(cca)

    wca = WorkspaceChannelAssociation(workspace_id="ws-1", channel_id="ch-1")
    with pytest.raises(ConflictError):
        await _association_on_pre_create(wca, req)


@pytest.mark.asyncio
async def test_multi_type_second_chat_conflicts_but_workspace_ok(tmp_path: Path):
    from primer.api.routers.channels import (
        _association_on_pre_create, _chat_association_on_pre_create,
    )
    from primer.model.except_ import ConflictError

    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await _seed_provider(
        p, ptype=ChannelProviderType.SLACK, cp_id="cp-1", ch_id="ch-1")

    req = _FakeRequest(p)
    cca = ChatChannelAssociation(channel_id="ch-1", default_agent_id="agent-x")
    await _chat_association_on_pre_create(cca, req)
    await p.get_storage(ChatChannelAssociation).create(cca)

    cca2 = ChatChannelAssociation(channel_id="ch-1", default_agent_id="agent-y")
    with pytest.raises(ConflictError):
        await _chat_association_on_pre_create(cca2, req)

    wca = WorkspaceChannelAssociation(workspace_id="ws-1", channel_id="ch-1")
    await _association_on_pre_create(wca, req)  # no raise
