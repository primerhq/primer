"""Offline SDK dispatch and shutdown at the final provider reference."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from primer.channel import retirement
from primer.model.channel import Channel, ChannelProvider, DiscordChannelProviderConfig
from primer.model.except_ import ProviderError
from tests.channel.test_channel_connections import _slack_provider, _tg_provider


class HeldCallback:
    """Expose cleanup that has started but has not yet finished."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.finish = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        self.entered.set()
        try:
            await self.finish.wait()
        finally:
            self.cleaning.set()
            await self.finish.wait()


def _room(provider, external_id):
    return Channel(id="room", provider_id=provider.id, provider=provider.provider, external_id=external_id)


def _slack_approval(app, ack, web):
    from slack_bolt.request.async_request import AsyncBoltRequest
    from slack_bolt.response import BoltResponse

    request = AsyncBoltRequest(
        mode="socket_mode",
        body={"channel": {"id": "C1"}, "actions": [{"value": "approve:w:s:t"}], "user": {"id": "U1"}},
        context={"ack": ack, "client": web},
    )
    listener = next(item for item in app._async_listeners if item.ack_function.__name__ == "_on_approve")
    return listener.run_ack_function(request=request, response=BoltResponse(status=200))


async def test_slack_bolt_approval_waiting_on_ack_cannot_reach_replacement(monkeypatch):
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    from primer.channel.slack.factory import _slack_factory

    monkeypatch.setattr(AsyncSocketModeHandler, "connect_async", AsyncMock())
    provider = _slack_provider("bolt-retire")
    room = _room(provider, "C1")
    adapter = await _slack_factory(provider, room, None)
    app = adapter._conn.app
    held = HeldCallback()
    web = app.client
    monkeypatch.setattr(web, "chat_update", AsyncMock())
    old_decision = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "_handle_decision", old_decision)
    # Bolt runs the registered ack function in its own task and injects its named arguments.
    callback = asyncio.create_task(_slack_approval(app, held, web))
    try:
        await asyncio.wait_for(held.entered.wait(), 2)
        closing = asyncio.create_task(adapter.aclose())
        await asyncio.wait_for(held.cleaning.wait(), 2)
        assert not closing.done()
        with pytest.raises(ProviderError, match="still closing"):
            await _slack_factory(provider, room, None)
    finally:
        held.finish.set()
        await asyncio.gather(callback, return_exceptions=True)
        await adapter.aclose()
    await closing
    assert callback.cancelled()
    replacement = await _slack_factory(provider, room, None)
    try:
        new_decision = AsyncMock(return_value=True)
        monkeypatch.setattr(replacement, "_handle_decision", new_decision)
        ack = AsyncMock()
        await _slack_approval(app, ack, web)  # an old, queued Bolt task begins after handoff
        ack.assert_not_awaited()
        old_decision.assert_not_awaited()
        new_decision.assert_not_awaited()
        await _slack_approval(replacement._conn.app, ack, web)
        new_decision.assert_awaited_once()
        web.chat_update.assert_awaited_once()
    finally:
        await replacement.aclose()


async def test_slack_socket_mode_detached_envelope_is_joined_before_close(monkeypatch):
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    from primer.channel.slack.factory import _slack_factory

    monkeypatch.setattr(AsyncSocketModeHandler, "connect_async", AsyncMock())
    provider = _slack_provider("socket-retire")
    room = _room(provider, "C1")
    adapter = await _slack_factory(provider, room, None)
    client = adapter._conn.handler.client
    held = HeldCallback()
    client.message_listeners.append(held)
    await client.enqueue_message('{}')  # SocketModeClient.process_message schedules the detached task
    try:
        await asyncio.wait_for(held.entered.wait(), 2)
        closing = asyncio.create_task(adapter.aclose())
        await asyncio.wait_for(held.cleaning.wait(), 2)
        assert not closing.done()
        with pytest.raises(ProviderError, match="still closing"):
            await _slack_factory(provider, room, None)
    finally:
        held.finish.set()
        await adapter.aclose()
    await closing
    assert client.message_processor.done() and client.aiohttp_client_session.closed


async def test_discord_gateway_and_modal_callbacks_are_fenced_at_handoff(monkeypatch):
    from primer.channel.discord import connection
    from primer.channel.discord.factory import _discord_factory
    from tests.channel.discord.test_factory import _interaction

    async def offline_gateway(client, token, *, retire_start):
        await client._async_setup_hook()
        return asyncio.create_task(asyncio.Event().wait())

    monkeypatch.setattr(connection, "_start_client_as_task", offline_gateway)
    provider = ChannelProvider(id="discord-retire", provider="discord",
                               config=DiscordChannelProviderConfig(bot_token=SecretStr("x" * 60)))
    room = _room(provider, "100")
    adapter = await _discord_factory(provider, room, None)
    client = adapter._client
    reject = _interaction("reject:w:s:t")
    await client.on_interaction(reject)
    modal = reject.response.send_modal.await_args.args[0]
    held = HeldCallback()
    monkeypatch.setattr(adapter, "_handle_decision", held)
    click = _interaction("approve:w:s:t")
    client.dispatch("interaction", click)  # discord.py schedules an independent _run_event task
    try:
        await asyncio.wait_for(held.entered.wait(), 2)
        closing = asyncio.create_task(adapter.aclose())
        await asyncio.wait_for(held.cleaning.wait(), 2)
        assert not closing.done()
        with pytest.raises(ProviderError, match="still closing"):
            await _discord_factory(provider, room, None)
    finally:
        held.finish.set()
        await adapter.aclose()
    await closing
    click.edit_original_response.assert_not_awaited()
    replacement = await _discord_factory(provider, room, None)
    try:
        new_decision = AsyncMock(return_value=True)
        monkeypatch.setattr(replacement, "_handle_decision", new_decision)
        late = _interaction("approve:w:s:t")
        await client.on_interaction(late)
        await modal.on_submit(late)
        late.response.defer.assert_not_awaited()
        new_decision.assert_not_awaited()
        await replacement._client.on_interaction(late)
        new_decision.assert_awaited_once()
    finally:
        await replacement.aclose()


@pytest.mark.parametrize("failed_stage", ["updater", "application"])
async def test_telegram_real_partial_stop_keeps_provider_quarantined(monkeypatch, failed_stage):
    from telegram.ext import Application, Updater

    from primer.channel.telegram import connection

    # Use the real PTB stop methods; stub initialization/start to avoid bot login and polling.
    async def mark_running(component, **kwargs):
        component._running = True

    monkeypatch.setattr(Application, "initialize", AsyncMock())
    monkeypatch.setattr(Application, "shutdown", AsyncMock())
    monkeypatch.setattr(Application, "start", mark_running)
    monkeypatch.setattr(Updater, "start_polling", mark_running)
    monkeypatch.setattr(retirement, "_pending", {})
    monkeypatch.setattr(retirement, "_RETRY_SECONDS", .005)
    provider = _tg_provider("ptb-partial-stop")
    pool = connection._TelegramConnectionRegistry()
    app = await pool.acquire(provider)
    unfinished = asyncio.create_task(asyncio.Event().wait())
    failure = AsyncMock(side_effect=RuntimeError("injected partial stop"))
    if failed_stage == "updater":
        # _stop_httpd precedes the SDK's polling-task join.
        app.updater._Updater__polling_task = unfinished
        monkeypatch.setattr(Updater, "_stop_httpd", failure)
    else:
        # Application.stop drains the job queue before joining its create_task set.
        app._job_queue = SimpleNamespace(stop=failure)
        app._Application__create_task_tasks.add(unfinished)
    stopped_component = Updater if failed_stage == "updater" else Application
    real_stop = stopped_component.stop
    refused_retry = asyncio.Event()

    async def observe_stop(component):
        try:
            return await real_stop(component)
        except RuntimeError as exc:
            if "not running" in str(exc):
                refused_retry.set()
            raise

    monkeypatch.setattr(stopped_component, "stop", observe_stop)
    closing = asyncio.create_task(pool.release(provider, expected=app))
    try:
        await asyncio.wait_for(refused_retry.wait(), 2)
        failure.assert_awaited_once()  # PTB refused the retry because it cleared running before the fault
        assert not app.running and not app.updater.running
        assert not closing.done() and not unfinished.done()
        with pytest.raises(ProviderError, match="still closing"):
            await pool.acquire(provider)
    finally:
        # End this isolated runtime. PTB cannot resume the interrupted stop on the same object.
        unfinished.cancel()
        pending = retirement._pending.get((pool, provider.id))
        if pending is not None:
            pending.cancel()
        await asyncio.gather(closing, unfinished, return_exceptions=True)


async def test_telegram_init_failure_never_starts_polling_and_releases_provider(monkeypatch):
    from telegram.ext import Application, Updater

    from primer.channel.telegram import connection

    monkeypatch.setattr(Application, "initialize", AsyncMock(side_effect=[RuntimeError("init failed"), None]))
    shutdown = AsyncMock()
    monkeypatch.setattr(Application, "shutdown", shutdown)
    app_stop, updater_stop = AsyncMock(), AsyncMock()
    monkeypatch.setattr(Application, "stop", app_stop)
    monkeypatch.setattr(Updater, "stop", updater_stop)
    pool = connection._TelegramConnectionRegistry()
    provider = _tg_provider("ptb-init-failure")
    with pytest.raises(RuntimeError, match="init failed"):
        await pool.acquire(provider)
    shutdown.assert_awaited_once()
    app = await pool.acquire(provider, inbound=False)
    await pool.release(provider, expected=app)
    assert shutdown.await_count == 2
    app_stop.assert_not_awaited()
    updater_stop.assert_not_awaited()
