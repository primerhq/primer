"""Actual SDK/adapter over in-memory transports; never a live model/socket."""
import asyncio
import json
import time

import httpx
from openai import AsyncOpenAI
import pytest
from starlette.requests import Request

from primer.llm.openchat import OpenChatLLM
from primer.model.chat import Done, Message, TextPart, Tool
from primer.model.except_ import ProviderTimeoutError
from primer.model.providers.llm import LLMProvider
from scripts.e2e.w4_f1 import contracts as c
from scripts.e2e.w4_f1.relay import Relay
from tests.tooling.test_w4_f1_contracts import wire


class DelayedBytes(httpx.AsyncByteStream):
    def __init__(self, data, delay=0):
        self.data, self.delay = data, delay

    async def __aiter__(self):
        await asyncio.sleep(self.delay)
        yield self.data


class IteratorBytes(httpx.AsyncByteStream):
    def __init__(self, iterator):
        self.iterator = iterator

    async def __aiter__(self):
        async for item in self.iterator:
            yield item


def body():
    return {'model': c.MODEL, 'stream': True, 'max_tokens': 1024,
            'messages': [{'role': 'system', 'content': c.BASE + '\n\nworkspace'}],
            'tools': [{'function': {'name': n}} for n in c.TOOLS]}


@pytest.mark.asyncio
@pytest.mark.parametrize('delay,stalls', [(6, False), (21, True)])
async def test_real_sdk_connect5_stall20_and_buffered_native_bytes(delay, stalls):
    async def upstream(request):
        assert json.loads(request.content)['model'] == c.MODEL
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=DelayedBytes(wire(), delay))
    relay = Relay('http://synthetic.invalid/v1', transport=httpx.MockTransport(upstream))
    relay.stage = 'cycle1'
    async def downstream(request):
        async def receive():
            return {'type': 'http.request', 'body': request.content, 'more_body': False}
        response = await relay.forward(Request({'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions', 'headers': []}, receive))
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=IteratorBytes(response.body_iterator))
    http = httpx.AsyncClient(transport=httpx.MockTransport(downstream))
    sdk = AsyncOpenAI(base_url='http://synthetic.invalid/v1', api_key='synthetic-no-secret', http_client=http, max_retries=0)
    provider = LLMProvider.model_validate({'id': 'f1-test', 'provider': 'openchat', 'config': {'url': 'http://synthetic.invalid/v1', 'flavor': 'vllm'},
                                         'limits': {'max_concurrency': 1, 'connect_timeout_seconds': 5, 'request_timeout_seconds': 20,
                                                    'total_timeout_seconds': 30, 'max_retries': 0}})
    llm = OpenChatLLM(provider)
    llm._client = sdk
    tools = [Tool(id=n, toolset_id=n.split('__')[0], description='Synthetic safe tool', args_schema={'type': 'object'}) for n in c.TOOLS]
    started = time.monotonic()
    try:
        async def consume():
            return [e async for e in llm.stream(model=c.MODEL, messages=[Message(role='system', parts=[TextPart(text=c.BASE + '\n\nworkspace')])],
                                                tools=tools, max_output_tokens=1024)]
        if stalls:
            with pytest.raises(ProviderTimeoutError) as exc:
                await consume()
            assert exc.value.code == 'stream_timeout'
            assert 19 <= time.monotonic() - started < 22
            # Disconnect did not prove upstream cancellation. No second request.
            assert relay.unknown_upstream and len(relay.packets) == 1
        else:
            events = await consume()
            assert any(isinstance(e, Done) for e in events)
            assert time.monotonic() - started >= 6  # More than actual connect5.
        await asyncio.gather(*relay.in_progress)
        assert not relay.unknown_upstream
        assert relay.packets[0]['response_hex'] == wire().hex()
        assert relay.packets[0]['disposition'] == 'forwarded'
    finally:
        for task in relay.in_progress:
            task.cancel()
        await asyncio.gather(*relay.in_progress, return_exceptions=True)
        await sdk.close()


@pytest.mark.asyncio
async def test_forbidden_genuine_exec_response_is_never_forwarded():
    raw = wire('workspace__exec', {'command': 'touch shared'})
    relay = Relay('http://synthetic.invalid/v1', transport=httpx.MockTransport(lambda r: httpx.Response(200, content=raw)))
    relay.stage = 'cycle1'
    response = await relay.exchange(body())
    assert response.status_code == 502 and relay.failure
    assert relay.packets[0]['response_hex'] == raw.hex()
    assert relay.packets[0]['disposition'] == 'rejected'
    assert not relay.unknown_upstream


@pytest.mark.asyncio
async def test_crash_hold_contains_real_complete_response_and_distinct_recovery_barrier():
    raw = wire('workspace__read', {'path': 'task.json'})
    relay = Relay('http://synthetic.invalid/v1', transport=httpx.MockTransport(lambda r: httpx.Response(200, content=raw)))
    relay.stage, relay.arm_crash = 'cycle2', True
    task = asyncio.create_task(relay.exchange(body()))
    await relay.held.wait()
    assert relay.packets[0]['upstream_complete'] and not relay.unknown_upstream and not task.done()
    relay.discard.set()
    assert (await task).status_code == 502
    assert relay.packets[0]['disposition'] == 'discarded_at_crash'
    relay.stage = 'recovery'
    recovered = asyncio.create_task(relay.exchange(body()))
    await relay.recovery_held.wait()
    assert not recovered.done()
    relay.release_recovery.set()
    assert (await recovered).status_code == 200
    assert relay.packets[1]['disposition'] == 'forwarded'


@pytest.mark.asyncio
async def test_concurrent_request_does_not_release_current_owner_or_create_upstream_attempt():
    release = asyncio.Event()
    async def upstream(request):
        await release.wait()
        return httpx.Response(200, content=wire())
    relay = Relay('http://synthetic.invalid/v1', transport=httpx.MockTransport(upstream))
    relay.stage = 'cycle1'
    first = asyncio.create_task(relay.exchange(body()))
    await asyncio.sleep(0)
    assert relay.busy
    second = await relay.exchange(body())
    assert second.status_code == 502 and relay.busy and len(relay.packets) == 1
    release.set()
    await first
    assert not relay.busy and relay.failure  # The overall run fails closed.


@pytest.mark.asyncio
async def test_budget_refuses_before_upstream_transport():
    def fail(request):
        pytest.fail('no network attempt is permitted after budget exhaustion')
    relay = Relay('http://synthetic.invalid/v1', transport=httpx.MockTransport(fail))
    relay.stage = 'cycle1'
    relay.packets = [{}] * c.MAX_REQUESTS
    assert (await relay.exchange(body())).status_code == 502


@pytest.mark.asyncio
async def test_outer_relay_deadline_keeps_unknown_upstream_and_does_not_retry(monkeypatch):
    from scripts.e2e.w4_f1 import relay as module
    assert module.RELAY_WALL_SECONDS == 35
    monkeypatch.setattr(module, 'RELAY_WALL_SECONDS', .03)
    attempts = []
    async def upstream(request):
        attempts.append(request)
        await asyncio.Event().wait()
    relay = Relay('http://synthetic.invalid/v1', transport=httpx.MockTransport(upstream))
    relay.stage = 'cycle1'
    response = await relay.exchange(body())
    assert response.status_code == 502 and 'TimeoutError' in relay.failure
    assert len(attempts) == 1 and relay.unknown_upstream and not relay.busy
    assert relay.packets[0]['upstream_complete'] is False
    assert (await relay.exchange(body())).status_code == 502 and len(attempts) == 1
