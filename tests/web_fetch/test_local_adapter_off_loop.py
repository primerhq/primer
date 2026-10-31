"""HTML extraction and the JSON pretty-print run off the event loop (W23); their output is unchanged."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

import primer.web_fetch.local as local_mod
from primer.web_fetch.local import LocalAdapter

HTML = (
    "<html><head><title> Pinned Page </title></head><body><nav>nav junk</nav><article><h1>Heading</h1>"
    "<p>" + ("The body sentence with a <a href='/x'>link</a>. " * 12) + "</p>"
    "<table><tr><th>k</th><th>v</th></tr><tr><td>a</td><td>1</td></tr></table></article></body></html>"
)


def _handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/j":
        return httpx.Response(200, headers={"content-type": "application/json"}, text='{"b":[1,2],"a":"é"}')
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=HTML)


def _adapter() -> LocalAdapter:
    return LocalAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)))


# --- characterization ---


@pytest.mark.asyncio
async def test_html_extraction_is_unchanged() -> None:
    page = await _adapter().fetch(url="https://example.com/a")
    sentence = "The body sentence with a [link](https://example.com/x). "
    assert page.content_markdown == "# Heading\n\n" + sentence * 12 + "\n\n| k | v | \n|---|---|\n| a | 1 |"
    assert (page.title, page.content_type, page.status, page.is_thin) == ("Pinned Page", "text/html", 200, False)


@pytest.mark.asyncio
async def test_json_pretty_print_is_unchanged() -> None:
    page = await _adapter().fetch(url="https://example.com/j")
    assert page.content_markdown == '```json\n{\n  "b": [\n    1,\n    2\n  ],\n  "a": "é"\n}\n```'
    assert (page.title, page.content_type) == ("", "application/json")


# --- W23 ---


async def _ticks_while(coro) -> int:
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        await coro
    finally:
        task.cancel()
    return ticks


@pytest.mark.asyncio
async def test_html_extraction_does_not_stop_the_loop(monkeypatch) -> None:
    real = LocalAdapter._extract_html

    def slow(self, *args):
        time.sleep(0.5)
        return real(self, *args)

    monkeypatch.setattr(LocalAdapter, "_extract_html", slow)
    assert await _ticks_while(_adapter().fetch(url="https://example.com/a")) >= 10


@pytest.mark.asyncio
async def test_json_pretty_print_does_not_stop_the_loop(monkeypatch) -> None:
    def slow_loads(text):
        time.sleep(0.5)
        return json.loads(text)

    monkeypatch.setattr(local_mod, "json", SimpleNamespace(loads=slow_loads, dumps=json.dumps))
    assert await _ticks_while(_adapter().fetch(url="https://example.com/j")) >= 10
