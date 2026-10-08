"""``http_request`` and the local web fetch stop reading a response at their byte cap and give up at a total deadline (architecture review A-10).

Both used to read the WHOLE body (``response.content``) and only then trim it, so a URL that streams gigabytes held them in memory, and the
httpx timeout is per operation, so a body that drips a byte at a time was bounded only by the remote server. A prompt-injected agent can point either
tool at such a URL. The bodies below are async generators that count what was pulled from them and stop on their own at a hard limit (so a broken
implementation here cannot eat the machine), served through ``httpx.MockTransport`` the way the neighbouring tests do.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from primer.toolset.web.tools import make_http_request_handler
from primer.web_fetch.adapter import WebFetchProviderError, WebFetchUnavailable
from primer.web_fetch.local import LocalAdapter

CAP = 100_000
CHUNK = 16_384
HARD_STOP = 64 * 1024 * 1024


class _Body:
    """A response body that records how much was pulled; ``closed`` says whether the response that carried it was closed."""

    def __init__(self, *, chunk: bytes = b"x" * CHUNK, delay: float = 0.0, hard_stop: int = HARD_STOP, until: int | None = None) -> None:
        self.pulled = 0
        self.response: httpx.Response | None = None
        self._chunk = chunk
        self._delay = delay
        self._hard_stop = hard_stop
        self._until = until

    @property
    def closed(self) -> bool:
        return self.response is not None and self.response.is_closed

    def stream(self):
        async def generator():
            while self.pulled < self._hard_stop and (self._until is None or self.pulled < self._until):
                self.pulled += len(self._chunk)
                yield self._chunk
                await asyncio.sleep(self._delay)

        return generator()


def _client(body: _Body, *, status: int = 200, content_type: str = "text/plain") -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body.response = httpx.Response(status, headers={"content-type": content_type}, content=body.stream())
        return body.response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- http_request ------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_request_reads_an_endless_body_only_to_its_cap():
    body = _Body()
    handler = make_http_request_handler(http_client=_client(body), response_body_byte_cap=CAP)

    result = await asyncio.wait_for(handler({"url": "https://example.com/"}), 10)

    assert not result.is_error
    payload = json.loads(result.output)
    assert payload["truncated"] is True and len(payload["body"]) == CAP
    assert body.pulled <= CAP + 2 * CHUNK, f"{body.pulled} bytes were pulled for a {CAP}-byte cap"
    assert body.closed, "the response was left open"


@pytest.mark.asyncio
async def test_http_request_gives_up_on_a_body_that_drips_past_its_total_deadline():
    body = _Body(chunk=b"x", delay=0.05)
    handler = make_http_request_handler(http_client=_client(body), response_body_byte_cap=CAP)
    started = time.monotonic()

    result = await asyncio.wait_for(handler({"url": "https://example.com/", "timeout_seconds": 0.3}), 10)

    assert result.is_error and "timed out" in result.output, result.output
    assert time.monotonic() - started < 3
    assert body.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("size, truncated", [(CAP - 1, False), (CAP, False), (CAP + 1, True)])
async def test_http_request_marks_a_body_truncated_only_when_more_followed_the_cap(size, truncated):
    body = _Body(chunk=b"y" * size, until=1)
    handler = make_http_request_handler(http_client=_client(body), response_body_byte_cap=CAP)

    payload = json.loads((await handler({"url": "https://example.com/"})).output)

    assert payload["truncated"] is truncated
    assert len(payload["body"]) == min(size, CAP)


# ---- the local web fetch -----------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_local_fetch_reads_an_endless_body_only_to_its_cap():
    body = _Body(chunk=b"a line of text\n" * 1000)
    adapter = LocalAdapter(client=_client(body), raw_byte_cap=CAP)

    page = await asyncio.wait_for(adapter.fetch(url="https://example.com/big.txt"), 10)

    assert 0 < len(page.content_markdown) <= CAP
    assert body.pulled <= CAP + 2 * len(b"a line of text\n" * 1000)
    assert body.closed, "the response was left open"


@pytest.mark.asyncio
async def test_the_local_fetch_gives_up_on_a_body_that_drips_past_its_total_deadline():
    body = _Body(chunk=b"x", delay=0.05)
    adapter = LocalAdapter(client=_client(body), raw_byte_cap=CAP, timeout=0.3)
    started = time.monotonic()

    with pytest.raises(WebFetchUnavailable, match="timed out"):
        await asyncio.wait_for(adapter.fetch(url="https://example.com/slow.txt"), 10)

    assert time.monotonic() - started < 3
    assert body.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status, error", [(500, WebFetchUnavailable), (403, WebFetchProviderError), (404, WebFetchProviderError)])
async def test_the_local_fetch_does_not_read_the_body_of_an_error_status(status, error):
    body = _Body()
    adapter = LocalAdapter(client=_client(body, status=status), raw_byte_cap=CAP)

    with pytest.raises(error):
        await asyncio.wait_for(adapter.fetch(url="https://example.com/x"), 10)

    assert body.pulled <= 2 * CHUNK, f"{body.pulled} bytes of an error page were read"
