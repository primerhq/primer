"""Failed provider startup at the real PTB initialize/getMe boundary, entirely offline."""

import asyncio

import pytest

from primer.channel import retirement
from primer.channel.telegram import connection
from primer.model.except_ import ProviderError
from tests.channel.test_channel_connections import _tg_provider


@pytest.fixture
async def offline_login(monkeypatch):
    """Run real Application, Bot and HTTP client lifecycles. Replace only Telegram's response."""
    from telegram.error import NetworkError
    from telegram.request import HTTPXRequest

    entered = asyncio.Event()
    answer = asyncio.Event()
    transports = []
    initialize = HTTPXRequest.initialize
    shutdown = HTTPXRequest.shutdown

    async def observe_initialize(request):
        await initialize(request)
        transports.append(request)

    async def get_me(request, url, **kwargs):
        assert url.endswith("/getMe")
        if not entered.is_set():
            entered.set()
            await answer.wait()
            raise NetworkError("offline login failure")
        return 200, b'{"ok":true,"result":{"id":7,"is_bot":true,"first_name":"Offline"}}'

    monkeypatch.setattr(HTTPXRequest, "initialize", observe_initialize)
    monkeypatch.setattr(HTTPXRequest, "do_request", get_me)
    try:
        yield entered, answer, transports
    finally:
        for request in transports:
            await shutdown(request)


@pytest.mark.parametrize("cancel_login", [False, True], ids=["error", "cancel"])
async def test_failed_real_initialize_closes_both_transports_before_reuse(offline_login, cancel_login):
    from telegram.error import NetworkError

    entered, answer, transports = offline_login
    pool = connection._TelegramConnectionRegistry()
    provider = _tg_provider("partial-initialize")
    attempt = asyncio.create_task(pool.acquire(provider, inbound=False))
    await asyncio.wait_for(entered.wait(), 2)
    assert len(transports) == 2 and all(not r._client.is_closed for r in transports)
    if cancel_login:
        attempt.cancel()
    else:
        answer.set()
    with pytest.raises(asyncio.CancelledError if cancel_login else NetworkError):
        await attempt
    assert all(r._client.is_closed for r in transports)
    replacement = await pool.acquire(provider, inbound=False)
    try:
        assert len(transports) == 4 and all(not r._client.is_closed for r in transports[2:])
    finally:
        await pool.release(provider, expected=replacement)


@pytest.mark.parametrize("uncertain", [False, True], ids=["retry-open-client", "incomplete-closed-client"])
async def test_failed_real_initialize_fences_incomplete_http_close(offline_login, monkeypatch, caplog, uncertain):
    from telegram.request import HTTPXRequest

    entered, answer, transports = offline_login
    pool = connection._TelegramConnectionRegistry()
    provider = _tg_provider("partial-close")
    attempt = asyncio.create_task(pool.acquire(provider, inbound=False))
    monkeypatch.setattr(retirement, "_pending", {})
    monkeypatch.setattr(retirement, "_RETRY_SECONDS", .005)
    await asyncio.wait_for(entered.wait(), 2)
    failed, peer = transports
    real_close = HTTPXRequest.shutdown
    attempted = asyncio.Event()
    retry_entered = asyncio.Event()
    allow_close = asyncio.Event()

    if uncertain:
        # httpx marks the client closed *before* its transport finishes closing.
        async def fail_transport_close():
            attempted.set()
            raise OSError("transport close interrupted")

        monkeypatch.setattr(failed._client._transport, "aclose", fail_transport_close)
    else:
        async def retryable_close(request):
            if request is failed:
                if not attempted.is_set():
                    attempted.set()
                    raise OSError("client still open")
                retry_entered.set()
                await allow_close.wait()
            await real_close(request)

        monkeypatch.setattr(HTTPXRequest, "shutdown", retryable_close)

    answer.set()
    pending = None
    try:
        await asyncio.wait_for(attempted.wait(), 2)
        pending = retirement._pending[(pool, provider.id)]
        if uncertain:
            # Wait through an actual retry, where PTB.shutdown would now return immediately.
            async with asyncio.timeout(2):
                while sum("teardown unresolved" in r.getMessage() for r in caplog.records) < 2:
                    await asyncio.sleep(.005)
        else:
            await asyncio.wait_for(retry_entered.wait(), 2)
        assert peer._client.is_closed
        assert failed._client.is_closed is uncertain
        # Cancelling the acquire waiter releases the pool lock. Retirement still owns the handle.
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        with pytest.raises(ProviderError, match="still closing"):
            await pool.acquire(provider, inbound=False)
        assert len(transports) == 2
        if not uncertain:
            allow_close.set()
            await asyncio.wait_for(asyncio.shield(pending), 2)
            assert failed._client.is_closed
            replacement = await pool.acquire(provider, inbound=False)
            await pool.release(provider, expected=replacement)
    finally:
        allow_close.set()
        attempt.cancel()
        if pending is not None and not pending.done():
            pending.cancel()
        tasks = [attempt] if pending is None else [attempt, pending]
        await asyncio.gather(*tasks, return_exceptions=True)
