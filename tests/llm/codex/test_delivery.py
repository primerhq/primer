"""Caller suspension cannot reuse admission from an already closed model socket.

All credentials, authority keys and model output here are synthetic. Hosted-only
acceptance uses the actual factory, client, broker and signed-policy code.
"""
import asyncio
import os
import time
from contextlib import aclosing
from dataclasses import replace
from types import SimpleNamespace

import pytest

from primer.api.registries.provider_registry import _build_default_llm_factory
from primer.llm.codex.boundary import BoundaryError, PolicySnapshot
from primer.llm.codex.broker import client, delivery
from primer.llm.codex.broker.http import RESPONSES
from primer.llm.codex.broker.service import BrokerServer
from primer.llm.codex.request import compact, decode, prepare_request
from tests.llm.codex import test_broker
from tests.llm.codex.test_bridge import frames, message, tool
from tests.llm.codex.test_broker import rsa_key as rsa_key, world as world
from tests.llm.codex.test_factory import binding as binding, local_peer as local_peer, row


@pytest.mark.asyncio
@pytest.mark.parametrize("after", ["tool_call_start", "tool_call_end", "usage"])
@pytest.mark.parametrize("change", ["revision", "suspend", "expire", "sign_out", "refresh"])
async def test_factory_terminal_suspension_rechecks_original_grant(
    world, binding, local_peer, monkeypatch, after, change,
):
    await world.login()
    monkeypatch.setattr(test_broker, "frames", lambda: frames(["files/read"]))
    server = BrokerServer(world.broker, peer_uid=os.geteuid(), account=world.account)
    listener = await asyncio.start_unix_server(server.handle, binding.trust_directory / "model.sock")
    try:
        async with listener, aclosing(_build_default_llm_factory(codex_brokers={"cx": binding})(row())) as llm:
            async with aclosing(llm.stream(model="model", messages=[message()], tools=[tool()])) as stream:
                async with asyncio.timeout(5):
                    while (await anext(stream)).type != after:
                        pass
                    # Terminal delivery only happens AFTER model EOF, not while
                    # its channel can still perform a policy check for us.
                    assert not world.broker._bridges
                    if change == "sign_out":
                        await world.auth.sign_out()
                    elif change == "refresh":
                        world.auth._record["expires_at"] = time.time() + 30
                        await world.auth.credential(world.account)
                    else:
                        patch = {"revision": "2"} if change == "revision" else (
                            {"suspended": True} if change == "suspend" else {"expires_at": time.time() - 1}
                        )
                        world.document.update(patch)
                        world.sign()
                    remaining = [event async for event in stream]
                assert len(remaining) == 1 and remaining[0].type == "error"
                assert remaining[0].fatal and remaining[0].code == "codex_boundary_error"
                assert sum(str(r.url) == RESPONSES for r in world.calls) == 1
                assert not llm.inner._bridge._active
    finally:
        listener.close()
        await listener.wait_closed()
        await server.close()
    assert not server._active and not world.broker._bridges


@pytest.mark.asyncio
async def test_factory_authorizes_each_terminal_event_without_new_inference(world, binding, local_peer, monkeypatch):
    await world.login()
    monkeypatch.setattr(test_broker, "frames", lambda: frames(["files/read"]))
    authorizations = []
    original = world.broker.authorize_delivery

    def authorize(*args):
        validity = original(*args)
        authorizations.append(args)
        return validity

    monkeypatch.setattr(world.broker, "authorize_delivery", authorize)
    server = BrokerServer(world.broker, peer_uid=os.geteuid(), account=world.account)
    listener = await asyncio.start_unix_server(server.handle, binding.trust_directory / "model.sock")
    try:
        async with listener, aclosing(_build_default_llm_factory(codex_brokers={"cx": binding})(row())) as llm:
            async with asyncio.timeout(5):
                events = [e async for e in llm.stream(model="model", messages=[message()], tools=[tool()])]
            assert [e.type for e in events] == ["text_delta", "tool_call_start", "tool_call_end", "usage", "done"]
            assert events[2].arguments == {"path": "a.txt"}
            assert len(authorizations) == 4
            assert len({args[1] for args in authorizations}) == 4
            assert len({args[2] for args in authorizations}) == 1
            assert sum(str(r.url) == RESPONSES for r in world.calls) == 1
    finally:
        listener.close()
        await listener.wait_closed()
        await server.close()


@pytest.mark.parametrize("change", ["account", "request", "step", "event", "revision", "generation", "expiry", "restart", "tamper", "deadline"])
def test_receipt_cannot_authorize_another_or_stale_delivery(monkeypatch, change):
    signer = delivery.DeliveryReceipts()
    policy = PolicySnapshot("account", "revision:generation", time.time() + 100, frozenset({"model"}))
    request = prepare_request("model", [message()])
    request_id = delivery.step_digest(request, "a" * 64)
    event = {"type": "done", "stop_reason": "end_turn"}
    deadline = time.monotonic() + 100
    receipt = signer.seal(account="account", model="model", policy=policy,
                          request=request_id, event=event, deadline=deadline)
    args = {"account": "account", "request": request_id, "event": delivery.digest(event), "current": policy}
    signer.authorize(receipt, **args)
    if change in {"account", "request", "event"}:
        args[change] = "b" * 64
    elif change == "step":
        args["request"] = delivery.step_digest(request, "b" * 64)
    elif change in {"revision", "generation"}:
        args["current"] = replace(policy, revision="other:" + change)
    elif change == "expiry":
        args["current"] = replace(policy, expires_at=0)
    elif change == "restart":
        signer = delivery.DeliveryReceipts()
    elif change == "tamper":
        receipt = receipt[:-1] + ("0" if receipt[-1] != "0" else "1")
    else:
        monkeypatch.setattr(delivery, "time", SimpleNamespace(monotonic=lambda: deadline + 1))
    with pytest.raises(BoundaryError):
        signer.authorize(receipt, **args)


@pytest.mark.asyncio
async def test_cancelled_delivery_admission_closes_its_peer(binding, local_peer):
    started, closed = asyncio.Event(), asyncio.Event()

    async def handle(reader, writer):
        try:
            request = decode(await reader.readline())
            assert request["method"] == "step/authorize"
            started.set()
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    listener = await asyncio.start_unix_server(handle, binding.trust_directory / "model.sock")
    instance = client.BrokerClient(binding.trust_directory, binding.manifest_sha256, row().limits)
    task = None
    try:
        async with listener, asyncio.timeout(5):
            task = asyncio.create_task(instance._authorize_delivery("receipt", "a" * 64, "b" * 64))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await closed.wait()
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        listener.close()
        await listener.wait_closed()
        await instance.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["expired_policy", "expired_step", "integer_true", "extra_field", "trailing_frame"])
async def test_delivery_reply_is_current_closed_and_strict(binding, local_peer, bad):
    reply = {"authorized": True, "expires_at": time.time() + 100, "deadline": time.monotonic() + 100}
    if bad == "expired_policy":
        reply["expires_at"] = 0
    elif bad == "expired_step":
        reply["deadline"] = 0
    elif bad == "integer_true":
        reply["authorized"] = 1
    elif bad == "extra_field":
        reply["unchecked"] = True
    finished = asyncio.Event()

    async def handle(reader, writer):
        try:
            await reader.readline()
            writer.write(compact(reply).encode() + b"\n")
            if bad == "trailing_frame":
                writer.write(b'{}\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    listener = await asyncio.start_unix_server(handle, binding.trust_directory / "model.sock")
    instance = client.BrokerClient(binding.trust_directory, binding.manifest_sha256, row().limits)
    try:
        async with listener, asyncio.timeout(5):
            with pytest.raises(BoundaryError):
                await instance._authorize_delivery("receipt", "a" * 64, "b" * 64)
            await finished.wait()
    finally:
        listener.close()
        await listener.wait_closed()
        await instance.aclose()


@pytest.mark.parametrize("mode", ["api", "worker"])
def test_documented_yaml_binding_reaches_application_config(tmp_path, monkeypatch, mode):
    from primer.model.scheduler import RuntimeMode
    from primer.cli import _load_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRIMER_CONFIG_PATH", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "codex_brokers:\n  codex-team:\n    trust_directory: /etc/primer/codex/team\n"
        '    manifest_sha256: "' + "a" * 64 + '"\n', encoding="utf-8",
    )
    config = _load_config(config_path, RuntimeMode(mode))
    binding = config.codex_brokers["codex-team"]
    assert str(binding.trust_directory) == "/etc/primer/codex/team"
    assert binding.manifest_sha256 == "a" * 64 and config.runtime_mode == RuntimeMode(mode)
