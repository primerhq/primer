"""W56j: the bot token is sent only to https://files.slack.com; any other url_private is not fetched."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from primer.channel.slack.adapter import SlackChannelAdapter


class _Client:
    gets: list[tuple[str, dict]] = []

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        type(self).gets.append((url, headers))
        return SimpleNamespace(status_code=200, content=b"data")


class _Store:
    async def put(self, *, data, mime_type, filename=None):
        return "artifact-1"


class _Artifacts:
    async def get_default(self):
        return _Store()


def _adapter() -> SlackChannelAdapter:
    provider = SimpleNamespace(id="cp", config=SimpleNamespace(bot_token=SimpleNamespace(get_secret_value=lambda: "xoxb-secret")))
    return SlackChannelAdapter(
        provider=provider, channel=SimpleNamespace(id="ch-1", external_id="C1"), inbox=None,
        storage_provider=object(), artifact_registry=_Artifacts(),
    )


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch):
    _Client.gets = []
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def _store(store, *, data, mime_type, filename=None, **kw):
        return SimpleNamespace(data=data, filename=filename)

    import primer.channel.media as media
    monkeypatch.setattr(media, "store_inbound_media", _store)


async def test_files_slack_com_is_fetched_with_the_token() -> None:
    url = "https://files.slack.com/files-pri/T1-F1/a.png"
    _text, parts = await _adapter()._collect_inbound_media("", [{"url_private_download": url, "name": "a.png"}])
    assert _Client.gets == [(url, {"Authorization": "Bearer xoxb-secret"})]
    assert len(parts) == 1


@pytest.mark.parametrize("url", [
    "https://attacker.example/x.png",
    "http://files.slack.com/files-pri/T1-F1/a.png",
    "https://files.slack.com.attacker.example/a.png",
    "https://files.slack.com@attacker.example/a.png",
])
async def test_other_hosts_are_not_fetched(url: str) -> None:
    _text, parts = await _adapter()._collect_inbound_media("", [{"url_private": url, "name": "a.png"}])
    assert _Client.gets == []
    assert parts == []
