"""Production factory and model IPC. Sign-in and host provisioning are synthetic."""
import asyncio
import json
import os
import socket
import threading
from contextlib import aclosing

import pytest

from primer.api.config import CodexBrokerBinding
from primer.api.registries.provider_registry import _build_default_llm_factory
from primer.llm.codex.broker import client
from primer.llm.codex.broker.http import RESPONSES
from primer.llm.codex.broker.service import BrokerServer
from primer.llm.codex.request import alias
from primer.model.except_ import ConfigError
from primer.model.provider import LLMProvider
from tests.llm.codex import test_broker
from tests.llm.codex.test_bridge import frames, message, tool
from tests.llm.codex.test_broker import rsa_key as rsa_key, world as world


def row(config=None, **limits):
    return LLMProvider.model_validate({
        "id": "cx", "provider": "codex", "config": config or {},
        "limits": {"max_concurrency": 1, "max_retries": 0, **limits},
    })


@pytest.fixture
def binding(tmp_path):
    return CodexBrokerBinding(trust_directory=tmp_path, manifest_sha256="a" * 64)


@pytest.fixture
def local_peer(monkeypatch, binding):
    # Replace only host provisioning/client-side identity. test_host exercises
    # the real root directory walk, manifest and kernel peer validation.
    class LocalPeer:
        identity = binding.manifest_sha256
        primer_uid = os.geteuid()

        def __init__(self, directory):
            assert directory == binding.trust_directory

        def connect_model_socket(self):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(binding.trust_directory / "model.sock"))
                sock.setblocking(False)
                return sock
            except BaseException:
                sock.close()
                raise

        def close(self):
            pass

    monkeypatch.setattr(client, "HostTrust", LocalPeer)
    return LocalPeer


def test_factory_requires_operator_binding_for_exact_row(binding):
    factory = _build_default_llm_factory(codex_brokers={"other": binding})
    with pytest.raises(ConfigError, match="No trusted Codex broker"):
        factory(row())


@pytest.mark.parametrize("config", [
    {"binary_path": "/bin/codex"}, {"codex_home": "/home/example"},
    {"model_catalog_path": "/etc/catalog.json"}, {"pass_env": ["LANG"]},
])
def test_factory_refuses_cli_overrides_before_host_io(monkeypatch, binding, config):
    monkeypatch.setattr(client, "HostTrust", lambda *_: pytest.fail("opened host trust"))
    with pytest.raises(ConfigError, match="overrides"):
        _build_default_llm_factory(codex_brokers={"cx": binding})(row(config))


@pytest.mark.parametrize("field,value", [("identity", "b" * 64), ("primer_uid", -1)])
def test_factory_checks_host_before_receiving_prompt(local_peer, binding, monkeypatch, field, value):
    monkeypatch.setattr(local_peer, field, value)
    monkeypatch.setattr(local_peer, "connect_model_socket", lambda *_: pytest.fail("connected"))
    with pytest.raises(ConfigError, match="host binding refused"):
        _build_default_llm_factory(codex_brokers={"cx": binding})(row())


@pytest.mark.asyncio
async def test_normal_factory_signed_broker_tool_call_and_policy_revocation(
    world, binding, local_peer, monkeypatch,
):
    await world.login()
    monkeypatch.setattr(test_broker, "frames", lambda: frames(["files/read"]))
    server = BrokerServer(world.broker, peer_uid=os.geteuid(), account=world.account)
    listener = await asyncio.start_unix_server(server.handle, binding.trust_directory / "model.sock")
    async with listener, aclosing(_build_default_llm_factory(codex_brokers={"cx": binding})(row())) as llm:
        async def step():
            return [event async for event in llm.stream(
                model="model", messages=[message("Read the file")], tools=[tool()], tool_choice="required",
            )]

        events = await step()
        assert [e.type for e in events] == ["text_delta", "tool_call_start", "tool_call_end", "usage", "done"]
        assert events[1].name == "files/read" and events[1].id == events[2].id
        assert events[2].arguments == {"path": "a.txt"} and events[-1].stop_reason == "tool_use"
        requests = [json.loads(r.content) for r in world.calls if str(r.url) == RESPONSES]
        assert len(requests) == 1 and requests[0]["model"] == "model"
        assert requests[0]["tools"][0]["tools"][0]["name"] == alias("files/read")
        world.document.update(revision="2", suspended=True)
        world.sign()
        refused, = await step()
        assert refused.type == "error" and refused.fatal and refused.code == "codex_boundary_error"
        assert sum(str(r.url) == RESPONSES for r in world.calls) == 1


@pytest.mark.asyncio
async def test_cancel_during_connect_disposes_late_socket(local_peer, binding, monkeypatch):
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    owned, peer = socket.socketpair()

    def delayed_connect(self):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        return owned

    monkeypatch.setattr(local_peer, "connect_model_socket", delayed_connect)
    try:
        async with aclosing(_build_default_llm_factory(codex_brokers={"cx": binding})(row())) as llm:
            async with aclosing(llm.stream(model="model", messages=[message()])) as stream:
                advancing = asyncio.create_task(anext(stream))
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    advancing.cancel()
                finally:
                    release.set()
                with pytest.raises(asyncio.CancelledError):
                    await advancing
                assert owned.fileno() == -1
    finally:
        release.set()
        owned.close()
        peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["first_frame_stall", "event_stall", "truncated", "refused", "consumer_close"])
async def test_incomplete_step_closes_peer_without_releasing_calls(local_peer, binding, end):
    loop = asyncio.get_running_loop()
    received = loop.create_future()

    async def handle(reader, writer):
        try:
            request = json.loads(await reader.readline())
            if end in {"event_stall", "consumer_close"}:
                writer.write(b'{"event":{"type":"text_delta","text":"hello","index":0}}\n')
            elif end in {"truncated", "refused"}:
                writer.write(b'{"event":{"type":"tool_call_start","id":"c","name":"files/read","index":0}}\n')
                writer.write(b'{"event":{"type":"tool_call_end","id":"c","arguments":{},"index":0}}\n')
                if end == "truncated":
                    writer.write(b'{"event":{"type":"done"')
                else:
                    writer.write(b'{"error":"broker_refused"}\n')
                writer.write_eof()
            await writer.drain()
            received.set_result((request, await reader.read()))
        except Exception as exc:
            received.set_exception(exc)
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_unix_server(handle, binding.trust_directory / "model.sock")
    limits = {"connect_timeout_seconds": 1} if end == "first_frame_stall" else {"request_timeout_seconds": 0.1}
    factory = _build_default_llm_factory(codex_brokers={"cx": binding})
    async with listener, aclosing(factory(row(**limits))) as llm, asyncio.timeout(5):
        async with aclosing(llm.stream(model="model", messages=[message()], tools=[tool()])) as stream:
            if end in {"event_stall", "consumer_close"}:
                assert (await anext(stream)).text == "hello"
            if end != "consumer_close":
                remaining = [event async for event in stream]
                assert len(remaining) == 1
                assert remaining[0].type == "error" and remaining[0].fatal
                assert remaining[0].code == "codex_boundary_error"
        request, trailing_input = await received
        assert request["method"] == "step/start" and trailing_input == b""
