"""A client that disconnects from a stream before its first chunk is not an unhandled exception (console review 2026-10-08, C-010).

Every closed workspace-tap connection logged ``unhandled exception in API request`` at ERROR with a full traceback: the stream's first chunk is late, the client goes
away, the app finishes without sending a response, and each of the three ``@app.middleware("http")`` layers (``BaseHTTPMiddleware``) raised
``RuntimeError("No response returned.")``, which reached the unhandled-exception handler. A disconnect is normal and silent. The middlewares now turn "no
response" into an empty 499 when the client really is gone (nobody receives it), and still raise it when the client is there (a real bug stays loud).

These use the production installers for the three middlewares plus the gzip layer in front of them, with a route whose first chunk is slow, driven with a
``receive`` that reports the disconnect.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI
from starlette.responses import Response, StreamingResponse

from primer.api._app_middleware import (
    _GZipExceptMcp,
    _install_console_csp,
    _install_request_id,
    _install_security_headers,
)
from primer.api.errors import register_error_handlers


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.add_middleware(_GZipExceptMcp, minimum_size=1024)
    _install_security_headers(app)
    _install_console_csp(app)
    _install_request_id(app)

    @app.get("/v1/_probe/slow_stream")
    async def slow_stream():
        async def gen():
            await asyncio.sleep(0.5)                 # the first chunk is late: the client can be gone before it
            yield b"data: x\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/v1/_probe/plain")
    async def plain():
        return {"ok": True}

    class _SendsNothing(Response):
        async def __call__(self, scope, receive, send) -> None:      # a handler bug: it "returns" a response and never sends it
            return None

    @app.get("/v1/_probe/sends_nothing")
    async def sends_nothing():
        return _SendsNothing()

    return app


async def _drive(app: FastAPI, path: str, *, client_leaves_after: float | None):
    sent: list[dict] = []

    async def receive():
        if client_leaves_after is None:
            await asyncio.Event().wait()
        await asyncio.sleep(client_leaves_after)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "headers": [(b"host", b"test")], "client": ("127.0.0.1", 1),
        "server": ("test", 80), "app": app,
    }
    task = asyncio.create_task(app(scope, receive, send))
    done, _ = await asyncio.wait({task}, timeout=5.0)
    if not done:
        task.cancel()
        pytest.fail("the request did not finish")
    return task, sent


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("leaves_after", [0.0, 0.1])
async def test_a_client_that_leaves_before_the_first_chunk_raises_nothing_and_logs_no_error(leaves_after: float, caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="primer.api.errors"):
        task, _sent = await _drive(_app(), "/v1/_probe/slow_stream", client_leaves_after=leaves_after)
    assert task.exception() is None, f"the disconnect escaped as {task.exception()!r}"
    assert not [r for r in caplog.records if "unhandled exception" in r.getMessage()], [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_an_ordinary_request_still_gets_its_headers_from_every_layer() -> None:
    task, sent = await _drive(_app(), "/v1/_probe/plain", client_leaves_after=None)
    assert task.exception() is None
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 200
    assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"
    assert headers["x-request-id"].startswith("req-")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_an_app_that_sends_nothing_while_the_client_is_still_there_is_still_a_loud_error(caplog) -> None:
    """The quiet 499 is only for a client that really left. A handler that returns without a response to a client that is still connected is a bug, and it
    still reaches the unhandled-exception handler: raised out of the app and logged at ERROR."""
    with caplog.at_level(logging.ERROR, logger="primer.api.errors"):
        task, _sent = await _drive(_app(), "/v1/_probe/sends_nothing", client_leaves_after=None)
    exc = task.exception()
    assert isinstance(exc, RuntimeError) and str(exc) == "No response returned.", f"the bug was swallowed: {exc!r}"
    assert [r for r in caplog.records if "unhandled exception" in r.getMessage() and r.levelno >= logging.ERROR], "and nothing was logged at ERROR"
