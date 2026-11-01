"""L7 / W56k: attachments download concurrently over one client, a declared oversize is never downloaded, Pillow runs off the loop."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest

from primer.channel import media
from primer.channel.discord.adapter import DiscordChannelAdapter
from primer.channel.slack.adapter import SlackChannelAdapter


class _Store:
    async def put(self, *, data, mime_type, filename=None):
        return f"artifact-{len(data)}"


class _Artifacts:
    async def get_default(self):
        return _Store()


def _slack() -> SlackChannelAdapter:
    provider = SimpleNamespace(id="cp", config=SimpleNamespace(bot_token=SimpleNamespace(get_secret_value=lambda: "tok")))
    return SlackChannelAdapter(
        provider=provider, channel=SimpleNamespace(id="ch", external_id="C1"), inbox=None,
        storage_provider=object(), artifact_registry=_Artifacts(),
    )


class _Http:
    instances = 0
    active = 0
    peak = 0
    gets: list[str] = []

    def __init__(self, *a, **k):
        type(self).instances += 1

    async def get(self, url, headers=None):
        cls = type(self)
        cls.gets.append(url)
        cls.active += 1
        cls.peak = max(cls.peak, cls.active)
        await asyncio.sleep(0.02)
        cls.active -= 1
        return SimpleNamespace(status_code=200, content=url.encode())

    async def aclose(self):
        type(self).closed = True


@pytest.fixture(autouse=True)
def _http(monkeypatch):
    _Http.instances = _Http.active = _Http.peak = 0
    _Http.gets = []
    monkeypatch.setattr(httpx, "AsyncClient", _Http)


def _files(n, **extra):
    return [{"url_private_download": f"https://files.slack.com/f{i}.png", "name": f"f{i}.png", "mimetype": "image/png", **extra} for i in range(n)]


async def test_slack_uses_one_client_downloads_concurrently_but_bounded_and_keeps_order():
    adapter = _slack()
    _text, parts = await adapter._collect_inbound_media("", _files(10))
    assert _Http.instances == 1
    assert 1 < _Http.peak <= media.ATTACHMENT_CONCURRENCY
    assert [p.artifact_id for p in parts] == [f"artifact-{len(f'https://files.slack.com/f{i}.png')}" for i in range(10)]
    await adapter.aclose()
    assert _Http.closed is True


async def test_slack_does_not_download_a_declared_oversize():
    _text, parts = await _slack()._collect_inbound_media("hi", _files(2, size=10**9))
    assert _Http.gets == [] and parts == []
    assert _text.count("[attachment skipped: too large]") == 2


async def test_discord_skips_a_declared_oversize_without_reading_it():
    reads = []

    class _Att:
        def __init__(self, name, size):
            self.filename, self.size, self.content_type = name, size, "application/pdf"

        async def read(self):
            reads.append(self.filename)
            return b"x" * 10

    adapter = DiscordChannelAdapter.__new__(DiscordChannelAdapter)
    adapter._artifacts, adapter._sp = _Artifacts(), object()
    parts, text = await adapter._build_media_parts([_Att("big.pdf", 10**9), _Att("ok.pdf", 10)], "")
    assert reads == ["ok.pdf"] and len(parts) == 1 and "too large" in text


async def test_compress_image_runs_off_the_event_loop(monkeypatch):
    seen = {}

    def spy(data, mime, cfg):
        seen["thread"] = threading.current_thread()
        return data, mime or "image/png"

    monkeypatch.setattr(media, "compress_image", spy)
    await media.store_inbound_media(_Store(), data=b"x" * 10, mime_type="image/png", filename="a.png")
    assert seen["thread"] is not threading.main_thread()
