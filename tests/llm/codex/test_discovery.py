"""Account discovery through real registry/client/server with synthetic host/auth.

These tests do not assert real identity provisioning or live model entitlement.
"""
import asyncio
import json
import os
from contextlib import aclosing

import pytest

from primer.api.registries.provider_registry import ProviderRegistry
from primer.llm.codex.boundary import BoundaryError
from primer.llm.codex.broker.client import BrokerClient
from primer.llm.codex.broker.service import BrokerServer
from primer.model.model_profile import ModelProfile
from primer.model.except_ import ConfigError
from primer.model.provider import LLMProvider
from primer.model_profile.checks import check_profile_on_create, check_profile_on_update
from tests.llm.codex.test_broker import rsa_key as rsa_key, world as world
from tests.llm.codex.test_factory import binding as binding, local_peer as local_peer, row


@pytest.mark.asyncio
async def test_signed_account_discovery_feeds_shared_profile_checks(world, binding, local_peer, fake_storage_provider):
    await world.login()
    provider = row()
    await fake_storage_provider.get_storage(LLMProvider).create(provider)
    registry = ProviderRegistry(fake_storage_provider, codex_brokers={provider.id: binding})
    server = BrokerServer(world.broker, peer_uid=os.geteuid(), account=world.account)
    listener = await asyncio.start_unix_server(server.handle, binding.trust_directory / "model.sock")
    async with listener, asyncio.timeout(5):
        models = await registry.discover_codex_models(provider)
        assert models == [{"name": "model", "display_name": "Test model"}]
        profile = ModelProfile(id="profile", description="real broker probe", provider_id=provider.id,
                               model_name="model", context_length=1000)
        await check_profile_on_create(profile, storage_provider=fake_storage_provider, provider_registry=registry)
        await check_profile_on_update(profile, profile, storage_provider=fake_storage_provider, provider_registry=registry)
        # A different policy removes the formerly available model, despite
        # unchanged provider configuration and a previously saved profile.
        world.document.update(revision="2", models=["other"])
        world.sign()
        assert await registry.discover_codex_models(provider) == []
        from primer.common.entity_checks import EntityCheckError
        with pytest.raises(EntityCheckError) as caught:
            await check_profile_on_update(profile, profile, storage_provider=fake_storage_provider, provider_registry=registry)
        assert caught.value.code == "codex_model_unavailable"
    assert not server._active


@pytest.mark.asyncio
async def test_discovery_requires_exact_host_binding_before_connection(binding, monkeypatch, fake_storage_provider):
    from primer.llm.codex.broker import client
    monkeypatch.setattr(client, "HostTrust", lambda *_: pytest.fail("opened unrelated host binding"))
    registry = ProviderRegistry(fake_storage_provider, codex_brokers={"other": binding})
    with pytest.raises(ConfigError, match="No trusted Codex broker"):
        await registry.discover_codex_models(row())


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    b'{"error":"broker_refused"}\n',
    b'{"models":[{"slug":"m","display_name":"M"}]}',
    b'{"models":[{"slug":"m","display_name":"M"}]}\n{}\n',
    b'{"models":[{"slug":"m","display_name":"M","context_length":1000}]}\n',
    b'{"models":[{"slug":"m","display_name":"M"},{"slug":"m","display_name":"M"}]}\n',
    b'{"models":[{"slug":"","display_name":"M"}]}\n',
    b'{"models":null}\n',
    b'x' * 1_048_577 + b'\n',
])
async def test_invalid_or_partial_catalogue_never_returns_models(binding, local_peer, response):
    finished = asyncio.Event()
    requests = []

    async def handle(reader, writer):
        try:
            requests.append(json.loads(await reader.readline()))
            writer.write(response)
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    listener = await asyncio.start_unix_server(handle, binding.trust_directory / "model.sock")
    async with listener, aclosing(BrokerClient(binding.trust_directory, binding.manifest_sha256, row().limits)) as client:
        with pytest.raises((BoundaryError, ValueError)):
            await client.models()
        await asyncio.wait_for(finished.wait(), 2)
        assert requests == [{"method": "models/list", "params": {}}]
        assert not client._discoveries


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "client_close", "stall"])
async def test_discovery_cancellation_and_stall_dispose_connection(binding, local_peer, ending):
    entered, finished = asyncio.Event(), asyncio.Event()

    async def handle(reader, writer):
        try:
            assert json.loads(await reader.readline()) == {"method": "models/list", "params": {}}
            entered.set()
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    listener = await asyncio.start_unix_server(handle, binding.trust_directory / "model.sock")
    limits = row(connect_timeout_seconds=0.5).limits
    async with listener, aclosing(BrokerClient(binding.trust_directory, binding.manifest_sha256, limits)) as client:
        caller_continued, finish_caller = asyncio.Event(), asyncio.Event()

        async def caller():
            try:
                return await client.models()
            except asyncio.CancelledError:
                if ending == "client_close":
                    caller_continued.set()
                    await finish_caller.wait()
                raise

        task = asyncio.create_task(caller())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if ending == "cancel":
                task.cancel()
            elif ending == "client_close":
                async with asyncio.timeout(2):
                    await client.aclose()
                    await caller_continued.wait()
                    await finished.wait()
                assert not task.done()
                finish_caller.set()
            with pytest.raises(TimeoutError if ending == "stall" else asyncio.CancelledError):
                await task
            await asyncio.wait_for(finished.wait(), 2)
            assert not client._discoveries
        finally:
            finish_caller.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_zero_discovery_deadline_expires_at_first_wait(binding, local_peer, monkeypatch):
    async def connect():
        await asyncio.sleep(0)
        pytest.fail("continued after a zero total timeout")

    limits = row(total_timeout_seconds=0).limits
    async with aclosing(BrokerClient(binding.trust_directory, binding.manifest_sha256, limits)) as client:
        monkeypatch.setattr(client, "_connected_socket", connect)
        with pytest.raises(TimeoutError):
            await client.models()
        assert not client._discoveries
