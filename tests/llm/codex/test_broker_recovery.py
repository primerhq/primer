"""Small offline crash-boundary and SSE accounting regressions. No live auth."""
import os
import stat
from urllib.parse import urlencode

import httpx
import pytest

from primer.llm.codex.boundary import BoundaryError
from primer.llm.codex.broker.auth import SignIn
from primer.llm.codex.broker.http import FixedHTTP
from primer.llm.codex.broker.security import PrivateStore
from tests.llm.codex.test_broker import rsa_key, world  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("target", ["revocations", "registration"])
@pytest.mark.parametrize("phase", ["before", "after"])
async def test_disposal_write_boundary_preserves_recoverable_handle(world, tmp_path, monkeypatch, staged, target, phase):
    if staged:
        params = world.start()
        await world.auth.complete(urlencode({"state": params["state"][0], "code": "x", "client_id": "issued-client"}))
    else:
        await world.login()
    write = world.store.write
    failed = []

    def fault(name, value):
        if name == target and not failed:
            failed.append(True)
            if phase == "after":
                write(name, value)
            raise OSError("synthetic persistence interruption")
        write(name, value)

    with monkeypatch.context() as patch:
        patch.setattr(world.store, "write", fault)
        with pytest.raises((OSError, BoundaryError)):
            await world.auth.sign_out()
    assert failed
    with pytest.raises(BoundaryError):
        world.auth.snapshot()
    queue = world.store.read("revocations")
    registration = world.store.read("registration")
    if not queue or not queue["items"]:
        # Before accepting durable disposal, original active credentials must
        # still exist. Never-committed staging is explicitly volatile.
        assert (registration is None if staged else "refresh_token" in registration)
    world.store.close()
    world.store = PrivateStore(tmp_path)
    restored = SignIn(world.store, world.policy, world.http)
    if queue and queue["items"]:
        with pytest.raises(BoundaryError):
            restored.snapshot()
        with pytest.raises(BoundaryError):
            await restored.credential(world.account)
        with pytest.raises(BoundaryError):
            restored.begin("http://127.0.0.1:12345/auth/callback")
        assert restored._selected["account"] == world.account
    attempts = []

    async def revoke(endpoint, client, refresh):
        attempts.append((client, refresh))

    monkeypatch.setattr(world.http, "revoke", revoke)
    await restored.sign_out()
    if not staged or queue and queue["items"]:
        assert attempts == [("issued-client", "dedicated-test-refresh")]
        assert restored._selected["account"] == world.account
    assert restored._record is None and restored._revocations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["revocations.json", "registration.json"])
@pytest.mark.parametrize("phase", ["replace-before", "replace-after", "file-fsync", "directory-fsync"])
async def test_disposal_actual_store_unknown_outcomes(world, tmp_path, monkeypatch, target, phase):
    await world.login()
    replace, fsync, write = os.replace, os.fsync, world.store.write
    active_name = None
    failed = []

    def wrapped_write(name, value):
        nonlocal active_name
        active_name = name + ".json"
        try:
            write(name, value)
        finally:
            active_name = None

    def wrapped_replace(src, dst, **kwargs):
        if dst == target and phase.startswith("replace") and not failed:
            failed.append(True)
            if phase == "replace-after":
                replace(src, dst, **kwargs)
            raise OSError("synthetic rename uncertainty")
        replace(src, dst, **kwargs)

    def wrapped_fsync(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if active_name == target and phase == ("directory-fsync" if is_directory else "file-fsync") and not failed:
            failed.append(True)
            raise OSError("synthetic fsync uncertainty")
        fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(world.store, "write", wrapped_write)
        patch.setattr(os, "replace", wrapped_replace)
        patch.setattr(os, "fsync", wrapped_fsync)
        with pytest.raises((OSError, BoundaryError)):
            await world.auth.sign_out()
    assert failed
    queue = world.store.read("revocations")
    original = world.store.read("registration")
    assert queue and queue["items"] or "refresh_token" in original
    world.store.close()
    world.store = PrivateStore(tmp_path)
    restored = SignIn(world.store, world.policy, world.http)
    if queue and queue["items"]:
        with pytest.raises(BoundaryError):
            restored.snapshot()
        with pytest.raises(BoundaryError):
            await restored.credential(world.account)
        with pytest.raises(BoundaryError):
            restored.begin("http://127.0.0.1:12345/auth/callback")
    attempts = []

    async def revoke(endpoint, client, refresh):
        attempts.append(refresh)

    monkeypatch.setattr(world.http, "revoke", revoke)
    await restored.sign_out()
    assert attempts == ["dedicated-test-refresh"]
    assert world.store.read("registration")["reauthorization_required"] is True
    assert not restored._revocations


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [False, True])
async def test_disposal_queue_completion_write_can_be_retried_after_restart(world, tmp_path, monkeypatch, after):
    await world.login()
    write = world.store.write
    attempts = []

    async def revoke(endpoint, client, refresh):
        attempts.append(refresh)

    def fault(name, value):
        if name == "revocations" and not value["items"]:
            if after:
                write(name, value)
            raise OSError("lost cleanup confirmation")
        write(name, value)

    monkeypatch.setattr(world.http, "revoke", revoke)
    with monkeypatch.context() as patch:
        patch.setattr(world.store, "write", fault)
        with pytest.raises(BoundaryError):
            await world.auth.sign_out()
    world.store.close()
    world.store = PrivateStore(tmp_path)
    restored = SignIn(world.store, world.policy, world.http)
    with pytest.raises(BoundaryError):
        restored.snapshot()
    await restored.sign_out()
    assert attempts == ["dedicated-test-refresh"] * (1 if after else 2)
    assert not restored._revocations


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"", b"x"])
async def test_sse_empty_and_short_lines_bounded_before_decode(monkeypatch, payload):
    from primer.llm.codex.broker import http
    monkeypatch.setattr(http, "MAX_EVENT_BYTES", 32, raising=False)
    entered, decoded, closed = [], [], []

    def decode(raw):
        decoded.append(raw)
        return {}

    monkeypatch.setattr(http, "decode", decode)

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(40):
                entered.append(i)
                yield b"data:" + payload + b"\n"
            yield b"\n"

        async def aclose(self):
            closed.append(True)

    client = FixedHTTP(_test_transport=httpx.MockTransport(lambda _: httpx.Response(
        200, stream=Body(), headers={"content-type": "text/event-stream"})))
    try:
        with pytest.raises(BoundaryError):
            _ = [event async for event in client.events({}, "synthetic")]
        assert decoded == [] and len(entered) <= (17 if payload else 34)
        assert closed == [True]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("one_byte", [False, True])
async def test_sse_multiline_reset_comments_and_done(monkeypatch, one_byte):
    from primer.llm.codex.broker import http
    monkeypatch.setattr(http, "MAX_EVENT_BYTES", 64, raising=False)
    parts = [b':comment\n\nevent: response\ndata: {"a":\n', b'data: 1}\n\n',
             b'data: {"b":2}\r\n\r\n', b'data: [DONE]\n\n']

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in parts:
                if one_byte:
                    for byte in part:
                        yield bytes([byte])
                else:
                    yield part
            raise AssertionError("DONE must stop before another read")

    client = FixedHTTP(_test_transport=httpx.MockTransport(lambda _: httpx.Response(
        200, stream=Body(), headers={"content-type": "text/event-stream"})))
    try:
        assert [event async for event in client.events({}, "synthetic")] == [{"a": 1}, {"b": 2}]
    finally:
        await client.close()
