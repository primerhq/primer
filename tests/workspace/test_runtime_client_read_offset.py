"""``RuntimeClient.read_file(path, offset=N)`` sends the offset only to a runtime that reported protocol 1.5 (perf plan Task 13, GI-15 folded in).

An older runtime ignores an argument it does not know and would return the WHOLE file, so the client never sends it there: it reads the whole
file as today and slices client-side. The answer is the same bytes either way.
"""
from __future__ import annotations

import base64

import pytest

from primer.workspace.runtime.protocol import OpName
from primer.workspace.runtime.runtime_client import RuntimeClient

CONTENT = b"0123456789"


def _client(version: str):
    client = RuntimeClient(url="ws://x/", token="t")
    client._negotiated_version = version
    sent: list[tuple[str, dict]] = []

    async def fake_send(op, args, **kw):
        sent.append((op, dict(args)))
        data = CONTENT[args["offset"]:] if "offset" in args else CONTENT
        return {"content_b64": base64.b64encode(data).decode()}

    client._send_request = fake_send  # type: ignore[assignment]
    return client, sent


@pytest.mark.asyncio
async def test_a_runtime_that_reported_1_5_is_sent_the_offset_and_returns_only_the_tail():
    client, sent = _client("1.5")
    assert await client.read_file("a.log", offset=4) == b"456789"
    assert sent == [(OpName.READ_FILE, {"path": "a.log", "offset": 4})]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["1.4", "1.3", "0.0", "garbage"])
async def test_an_older_runtime_is_never_sent_the_offset_but_the_answer_is_the_same(version):
    client, sent = _client(version)
    assert await client.read_file("a.log", offset=4) == b"456789"
    assert sent == [(OpName.READ_FILE, {"path": "a.log"})]


@pytest.mark.asyncio
async def test_a_newer_runtime_than_1_5_counts_as_agreeing():
    client, sent = _client("1.10")
    assert await client.read_file("a.log", offset=2) == b"23456789" and sent[0][1].get("offset") == 2


@pytest.mark.asyncio
async def test_no_offset_is_the_plain_read_on_every_runtime():
    for version in ("1.4", "1.5"):
        client, sent = _client(version)
        assert await client.read_file("a.log") == CONTENT and sent == [(OpName.READ_FILE, {"path": "a.log"})]
