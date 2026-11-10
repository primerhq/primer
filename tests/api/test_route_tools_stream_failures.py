"""Cancellation ownership, terminal bytes and private stream failure contracts."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from mcp.types import CallToolRequestParams
from starlette.responses import Response

from primer.access.acting import acting_as
from primer.access.person import Person
from primer.api import route_tools
from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.server import build_mcp_server, current_actor
from primer.model.principal import Principal
from tests.mcp.conftest import FakeProviderRegistry


async def _call(snapshot):
    with acting_as(Person(None, "system")):
        return await snapshot.providers["misc"].call(tool_name="stream_probe", arguments={})


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["single_cancel", "double_cancel", "timeout_cancel"])
async def test_caller_cancellation_drains_without_recancelling_generator(termination, monkeypatch):
    app = FastAPI()
    producing = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def chunks():
        try:
            yield b"first"
            producing.set()
            await asyncio.Event().wait()
        finally:
            closing.set()
            await release.wait()
            cleaned.set()

    @app.get("/stream")
    @route_tools.route_tool(scoped_id="misc__stream_probe")
    async def stream() -> StreamingResponse:
        return StreamingResponse(chunks())

    snapshot = route_tools.build_generated_route_toolsets(app)
    if termination == "timeout_cancel":
        monkeypatch.setattr(route_tools, "_DISPATCH_TIMEOUT_S", 0.02)
    task = asyncio.create_task(_call(snapshot))
    try:
        await asyncio.wait_for(producing.wait(), 1)
        if termination != "timeout_cancel":
            task.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        if termination != "single_cancel":
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set(), "a caller cancellation interrupted the producer's finalizer"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_size_bound_cancels_producer_with_stale_cancellation_count(monkeypatch):
    app = FastAPI()
    cleaned = asyncio.Event()

    async def chunks():
        # A provider may have previously handled cancellation itself. Task's
        # historical counter says nothing about who owns this transport abort.
        asyncio.current_task().cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        try:
            yield b"x" * 1_048_577
            pytest.fail("the crossing chunk must stop the producer")
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    @app.get("/stream")
    @route_tools.route_tool(scoped_id="misc__stream_probe")
    async def stream() -> StreamingResponse:
        return StreamingResponse(chunks())

    snapshot = route_tools.build_generated_route_toolsets(app)
    monkeypatch.setattr(route_tools, "_DISPATCH_TIMEOUT_S", 0.05)
    result = await _call(snapshot)
    assert result.is_error
    assert json.loads(result.output)["type"] == "route-tool-response-too-large"
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_post_terminal_body_is_not_part_of_generated_response():
    app = FastAPI()
    specs = []

    class LateBody(Response):
        async def __call__(self, scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"complete", "more_body": False})
            await send({"type": "http.response.body", "body": b"late", "more_body": True})

    @app.get("/stream")
    @route_tools.route_tool(scoped_id="misc__stream_probe")
    async def stream(request: Request) -> Response:
        specs.append(request.scope["asgi"].get("spec_version"))
        return LateBody()

    snapshot = route_tools.build_generated_route_toolsets(app)
    result = await _call(snapshot)
    assert not result.is_error
    assert base64.b64decode(json.loads(result.output)["body_base64"]) == b"complete"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://external") as client:
        # HTTPX's external test transport rejects this invalid application;
        # the private response-complete rule must not alter that transport.
        with pytest.raises(AssertionError):
            await client.get("/stream")
    assert specs == ["2.4", None], "the private spec must not affect external request scopes"


@pytest.mark.asyncio
async def test_send_after_transport_abort_reports_closed_connection():
    app = FastAPI()
    rejected = asyncio.Event()

    class ContinuesAfterAbort(Response):
        async def __call__(self, scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            try:
                await send({"type": "http.response.body", "body": b"x" * 1_048_577, "more_body": True})
            except asyncio.CancelledError:
                with pytest.raises(OSError, match="connection is closed"):
                    await send({"type": "http.response.body", "body": b"late", "more_body": False})
                rejected.set()

    @app.get("/stream")
    @route_tools.route_tool(scoped_id="misc__stream_probe")
    async def stream() -> Response:
        return ContinuesAfterAbort()

    snapshot = route_tools.build_generated_route_toolsets(app)
    result = await _call(snapshot)
    assert json.loads(result.output)["type"] == "route-tool-response-too-large"
    assert rejected.is_set()


@pytest.mark.asyncio
async def test_repeated_cancel_has_a_bounded_wait_and_retains_cleanup(monkeypatch):
    app = FastAPI()
    producing = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def chunks():
        try:
            producing.set()
            await asyncio.Event().wait()
            yield b"never"
        finally:
            closing.set()
            await release.wait()
            cleaned.set()

    @app.get("/stream")
    @route_tools.route_tool(scoped_id="misc__stream_probe")
    async def stream() -> StreamingResponse:
        return StreamingResponse(chunks())

    snapshot = route_tools.build_generated_route_toolsets(app)
    monkeypatch.setattr(route_tools, "_STREAM_CLEANUP_TIMEOUT_S", 0.02)
    task = asyncio.create_task(_call(snapshot))
    try:
        await asyncio.wait_for(producing.wait(), 1)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.5)
        assert task in done, "caller exceeded the cleanup wait bound"
        with pytest.raises(asyncio.CancelledError):
            task.result()
        assert not cleaned.is_set()
    finally:
        release.set()
        await asyncio.wait_for(cleaned.wait(), 1)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_failure", ["error", "timeout"])
async def test_producer_error_survives_failing_close(close_failure, monkeypatch):
    app = FastAPI()

    class ProducerError(Exception):
        pass

    class CleanupError(Exception):
        pass

    primary = ProducerError("primary producer failure")

    class Body:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise primary

        async def aclose(self):
            if close_failure == "timeout":
                await asyncio.Event().wait()
            raise CleanupError("secondary cleanup failure")

    @app.get("/stream")
    @route_tools.route_tool(scoped_id="misc__stream_probe")
    async def stream() -> StreamingResponse:
        return StreamingResponse(Body())

    route_tools.build_generated_route_toolsets(app)
    monkeypatch.setattr(route_tools, "_STREAM_CLEANUP_TIMEOUT_S", 0.01)
    transport = route_tools._BoundedAsgiTransport(  # noqa: SLF001
        route_tools._TrustedAsgiApp(app, Person(None, "system")),  # noqa: SLF001
        max_response_bytes=1_048_576,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://internal") as client:
        with pytest.raises(Exception) as raised:
            await client.get("/stream")
    assert raised.value.__cause__ is primary, "transport lost the original producer exception"
    assert "primary producer failure" not in str(raised.value)


@pytest.mark.asyncio
async def test_arbitrary_late_stream_secret_is_bounded_through_real_mcp(fake_storage_provider):
    app = FastAPI()
    secret = "arbitrary_stream_password_sentinel"

    async def chunks():
        yield b"partial audio"
        raise RuntimeError(secret * 10_000)

    @app.get("/stream")
    @route_tools.route_tool(scoped_id="misc__stream_probe", read_only=True)
    async def stream() -> StreamingResponse:
        return StreamingResponse(chunks())

    snapshot = route_tools.build_generated_route_toolsets(app)
    deps = ExposureDeps(
        storage_provider=fake_storage_provider,
        provider_registry=FakeProviderRegistry(dict(snapshot.providers)),
    )
    await update_exposure(enabled=True, allowed_tools=["misc__stream_probe"], updated_by="alice", deps=deps)
    handler = build_mcp_server(lambda: deps).get_request_handler("tools/call").handler
    token = current_actor.set(Principal(type="system", id="test-system", display="system", source="internal"))
    try:
        result = await handler(
            None,
            CallToolRequestParams(
                name="call_tool",
                arguments={"scoped_id": "misc__stream_probe", "arguments": {}},
            ),
        )
    finally:
        current_actor.reset(token)
    assert result.is_error
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "tool_failed"
    message = payload["error"]["message"]
    assert secret not in message
    assert len(message) < 256
    assert json.loads(message) == {
        "type": "route-tool-stream-failed",
        "message": "generated REST response stream failed",
    }
