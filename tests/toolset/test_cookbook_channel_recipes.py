"""Every cookbook recipe's channel steps run through the real system tools exactly as printed (ticket 01a11526). Five recipes printed a
``system::create_channel`` body without the required ``provider`` and failed as written, and nothing noticed because the cookbook
tests check headings only. This is the guard: it extracts each recipe's ``create_channel_provider`` and ``create_channel`` json
bodies and executes them. A recipe that names a provider it does not create (the digest recipe assumes ``slack-1`` exists) gets one
of the platform its channel declares, so the channel step is still exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.model.channel import (
    ChannelProvider,
    ChannelProviderType,
    DiscordChannelProviderConfig,
    SlackChannelProviderConfig,
    TelegramChannelProviderConfig,
)
from tests.docs.test_docs_tool_call_bodies import calls_in
from tests.toolset.test_system_crud_guards import _call, world  # noqa: F401  (world is a fixture)

COOKBOOK = Path(__file__).resolve().parents[2] / "docs" / "wiki" / "cookbook"


def _body(text: str, tool: str) -> dict | None:
    return next((call.arguments for call in calls_in(text) if call.toolset == "system" and call.tool == tool), None)


def _recipes() -> list[tuple[str, dict | None, dict]]:
    out = []
    for path in sorted(COOKBOOK.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        channel = _body(text, "create_channel")
        if channel is not None:
            out.append((path.name, _body(text, "create_channel_provider"), channel))
    return out


def _provider_row(provider_id: str, platform: str) -> ChannelProvider:
    config = {
        "slack": SlackChannelProviderConfig(app_token=SecretStr("xapp-1-live-0123456789"), bot_token=SecretStr("xoxb-live-9876543210")),
        "discord": DiscordChannelProviderConfig(bot_token=SecretStr("d" * 40)),
        "telegram": TelegramChannelProviderConfig(bot_token=SecretStr("123456789:ABCDEFGHIJKLMNOPQRST")),
    }[platform]
    return ChannelProvider(id=provider_id, provider=ChannelProviderType(platform), config=config)


def test_the_extraction_finds_the_recipes() -> None:
    # A regex that finds nothing would make every test below vanish and "pass".
    names = [name for name, _, _ in _recipes()]
    assert len(names) >= 5, f"expected at least the five recipes that create a channel, found {names}"


def test_the_canonical_channel_recipes_remain_in_the_executable_inventory() -> None:
    assert {
        "approval-gated-deploy-bot.md", "daily-incident-digest.md", "discord-moderation-helper.md",
        "slack-question-answerer.md", "telegram-personal-assistant.md",
    } <= {name for name, _, _ in _recipes()}


def test_wiki_channel_calls_select_the_exact_system_operation_and_arguments() -> None:
    text = '''`system__create_channel` names the channel.
```json
{"toolset_id": "other", "tool_name": "create_channel", "arguments": {"entity": {"id": "wrong-toolset"}}}
```
```text
{"toolset_id": "system", "tool_name": "create_channel", "arguments": {"entity": {"id": "not-a-call"}}}
```
```json
{"id": "response", "provider": "slack"}
```
```json
{"toolset_id": "system", "tool_name": "create_channel_provider", "arguments": {"entity": {"id": "provider"}}}
```
```json
{"toolset_id": "system", "tool_name": "create_channel", "arguments": {"entity": {"id": "channel", "provider_id": "provider", "provider": "slack"}}}
```
'''
    assert _body(text, "create_channel") == {"entity": {"id": "channel", "provider_id": "provider", "provider": "slack"}}
    assert _body(text, "create_channel_provider") == {"entity": {"id": "provider"}}
    assert _body(text, "delete_channel") is None


@pytest.mark.parametrize("name,provider,channel", _recipes(), ids=[name for name, _, _ in _recipes()])
@pytest.mark.asyncio
async def test_a_recipes_channel_steps_succeed_as_written(world, name, provider, channel) -> None:
    sp, toolset, _ = world
    entity = channel["entity"]
    if provider is not None:
        provider_error, provider_answer = await _call(toolset, "create_channel_provider", **provider)
        assert not provider_error, f"{name}: the provider step fails as written: {provider_answer}"
    else:
        # The recipe assumes the provider exists. Seed one of the platform the channel body declares (slack when it declares
        # none, so a body that forgot its provider is refused by the TOOL below, not by this setup).
        await sp.get_storage(ChannelProvider).create(_provider_row(entity["provider_id"], entity.get("provider", "slack")))

    channel_error, channel_answer = await _call(toolset, "create_channel", **channel)

    assert not channel_error, f"{name}: the channel step fails as written: {channel_answer}"
