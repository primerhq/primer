"""Two editors holding one etag cannot both write (review of workspaces, W55b).

The read computed its etag AFTER reading the bytes (a write in between left an etag newer than the content the client holds), and the write checked
the precondition OUTSIDE any lock (two requests with one etag both passed it). Now the etag comes from ``file_info`` taken BEFORE the read, and a per
(workspace, path) lock covers the precondition and the write, so the second of two editors answers 412.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tests.api.test_workspace_files_studio import _setup, app, client, pr, sp, wsr  # noqa: F401 - the fixtures of the file-IO tests

pytestmark = pytest.mark.asyncio


async def _put(client, wid, content, **params):
    return await client.put(
        f"/v1/workspaces/{wid}/files", params={"path": "doc.txt", **params}, json={"content": content, "encoding": "text"},
    )


async def _etag(client, wid) -> str:
    return (await client.get(f"/v1/workspaces/{wid}/files/read", params={"path": "doc.txt"})).json()["etag"]


def _slow_writes(ws, delay: float = 0.05):
    real = ws.write_file

    async def slow(path, content):
        await asyncio.sleep(delay)  # a real write takes time: the window in which a second precondition check used to pass
        await real(path, content)

    ws.write_file = slow


async def test_the_second_of_two_editors_with_one_etag_answers_412(client, wsr):
    wid, ws = await _setup(client, wsr)
    assert (await _put(client, wid, "v0")).status_code == 204
    etag = await _etag(client, wid)
    _slow_writes(ws)

    first, second = await asyncio.gather(_put(client, wid, "editor A", etag=etag), _put(client, wid, "editor B", etag=etag))

    assert sorted([first.status_code, second.status_code]) == [204, 412], (first.text, second.text)
    winner = "editor A" if first.status_code == 204 else "editor B"
    assert (await client.get(f"/v1/workspaces/{wid}/files/read", params={"path": "doc.txt"})).json()["content"] == winner


async def test_the_etag_of_a_read_is_taken_before_the_bytes_are_read(client, wsr):
    """A write that lands between the stat and the read leaves the client with an etag of the OLDER state: its own write with it is refused."""
    wid, ws = await _setup(client, wsr)
    assert (await _put(client, wid, "v0")).status_code == 204
    real_read = ws.read_file

    async def read_then_a_concurrent_write(path):
        raw = await real_read(path)
        ws._files[path] = b"v9"  # a same-size edit, landing after the bytes were read (a different size would show in the etag anyway)
        ws._mtimes[path] = datetime.now(timezone.utc) + timedelta(seconds=5)
        return raw

    ws.read_file = read_then_a_concurrent_write
    read = await client.get(f"/v1/workspaces/{wid}/files/read", params={"path": "doc.txt"})
    ws.read_file = real_read

    assert read.json()["content"] == "v0"
    resp = await _put(client, wid, "my edit of v0", etag=read.json()["etag"])
    assert resp.status_code == 412, "the edit would have clobbered the concurrent write"
    assert (await client.get(f"/v1/workspaces/{wid}/files/read", params={"path": "doc.txt"})).json()["content"] == "v9"


async def test_a_write_with_the_current_etag_and_one_with_no_precondition_still_work(client, wsr):
    wid, _ws = await _setup(client, wsr)
    assert (await _put(client, wid, "v0")).status_code == 204
    etag = await _etag(client, wid)

    assert (await _put(client, wid, "v1", etag=etag)).status_code == 204
    assert (await _put(client, wid, "v2")).status_code == 204
    assert (await _put(client, wid, "v3", etag=etag)).status_code == 412, "the etag of v0 is stale now"


async def test_the_read_etag_equals_the_write_check_etag_for_an_unchanged_file(client, wsr):
    wid, _ws = await _setup(client, wsr)
    assert (await _put(client, wid, "v0")).status_code == 204

    assert await _etag(client, wid) == await _etag(client, wid)
    assert (await _put(client, wid, "same file", etag=await _etag(client, wid))).status_code == 204


async def test_writes_to_different_paths_do_not_serialize_each_other(client, wsr):
    wid, ws = await _setup(client, wsr)
    real = ws.write_file
    started: list[str] = []

    async def gated(path, content):
        started.append(path)
        while len(started) < 2:  # both writes must be in flight at once, or this never ends
            await asyncio.sleep(0.01)
        await real(path, content)

    ws.write_file = gated

    async def put(path):
        return await client.put(f"/v1/workspaces/{wid}/files", params={"path": path}, json={"content": "x", "encoding": "text"})

    first, second = await asyncio.wait_for(asyncio.gather(put("a.txt"), put("b.txt")), 5)

    assert (first.status_code, second.status_code) == (204, 204)
