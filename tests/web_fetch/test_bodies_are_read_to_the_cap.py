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
import tracemalloc
import zlib

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


# ---- compressed bodies: the cap bounds each DECODE STEP, not only the bytes kept (review of this PR) -----------------------------------------------


def _gzip(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def _zlib(data: bytes) -> bytes:
    return zlib.compress(data, 9)


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def _bomb(size: int) -> bytes:
    """``size`` zero bytes, gzip-compressed in pieces so building it does not itself need ``size`` bytes at once."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    out = bytearray()
    piece = b"\0" * (1 << 20)
    for _ in range(size >> 20):
        out += compressor.compress(piece)
    return bytes(out + compressor.flush())


class _RawStream(httpx.AsyncByteStream):
    """The wire bytes of a response, in pieces, NOT yet read: what a real transport hands httpx.

    ``httpx.Response(content=<bytes>)`` reads and DECODES the body when it is built, which would put the inflation inside the test's own handler
    (and make ``aiter_raw`` raise ``StreamConsumed``); a custom stream is left for the code under test to read.
    """

    def __init__(self, body: bytes, piece: int = 64 * 1024) -> None:
        self._body, self._piece = body, piece

    async def __aiter__(self):
        for start in range(0, len(self._body), self._piece):
            yield self._body[start:start + self._piece]


def _encoded_client(body: bytes, encoding: str | None, seen: list | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        headers = {"content-type": "text/plain"}
        if encoding:
            headers["content-encoding"] = encoding
        return httpx.Response(200, headers=headers, stream=_RawStream(body))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["http_request", "local_fetch"])
async def test_a_compression_bomb_is_not_inflated_past_the_cap_in_one_step(which):
    """64 MiB of zeros is about 64 KB gzipped: ONE raw chunk. Decoding it whole holds 64 MiB before anything can trim it."""
    bomb = _bomb(64 << 20)
    assert len(bomb) < 1 << 20, "premise: the bomb is small on the wire"
    client = _encoded_client(bomb, "gzip")
    tracemalloc.start()
    try:
        if which == "http_request":
            handler = make_http_request_handler(http_client=client, response_body_byte_cap=CAP)
            payload = json.loads((await asyncio.wait_for(handler({"url": "https://example.com/"}), 20)).output)
            assert payload["truncated"] is True and len(payload["body"]) == CAP
        else:
            page = await asyncio.wait_for(LocalAdapter(client=client, raw_byte_cap=CAP).fetch(url="https://example.com/bomb.txt"), 20)
            assert len(page.content_markdown) <= CAP
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 8 * CAP + (1 << 20), f"the decode step held {peak} bytes for a {CAP}-byte cap"


@pytest.mark.asyncio
@pytest.mark.parametrize("compress", [_gzip, _zlib, _raw_deflate], ids=["gzip", "deflate", "raw-deflate"])
async def test_a_compressed_body_is_decoded_and_the_cap_counts_the_decoded_bytes(compress):
    text = ("line of text\n" * 50_000).encode()           # 650 KB decoded, a few KB on the wire
    encoding = "gzip" if compress is _gzip else "deflate"
    handler = make_http_request_handler(http_client=_encoded_client(compress(text), encoding), response_body_byte_cap=CAP)

    payload = json.loads((await handler({"url": "https://example.com/"})).output)

    assert payload["truncated"] is True and payload["body"] == text[:CAP].decode()
    whole = make_http_request_handler(http_client=_encoded_client(compress(text), encoding), response_body_byte_cap=len(text))
    whole_payload = json.loads((await whole({"url": "https://example.com/"})).output)
    assert whole_payload["truncated"] is False and whole_payload["body"] == text.decode()


@pytest.mark.asyncio
async def test_a_body_with_no_content_encoding_is_read_as_it_is():
    handler = make_http_request_handler(http_client=_encoded_client(b"plain body", None), response_body_byte_cap=CAP)

    assert json.loads((await handler({"url": "https://example.com/"})).output)["body"] == "plain body"


@pytest.mark.asyncio
async def test_http_request_refuses_an_encoding_it_cannot_bound():
    handler = make_http_request_handler(http_client=_encoded_client(b"\x1b\x00", "br"), response_body_byte_cap=CAP)

    result = await handler({"url": "https://example.com/"})

    assert result.is_error and "unsupported content-encoding" in result.output and "br" in result.output, result.output


@pytest.mark.asyncio
async def test_the_local_fetch_refuses_an_encoding_it_cannot_bound():
    adapter = LocalAdapter(client=_encoded_client(b"\x1b\x00", "br"), raw_byte_cap=CAP)

    with pytest.raises(WebFetchProviderError, match="unsupported content-encoding"):
        await adapter.fetch(url="https://example.com/x.txt")


@pytest.mark.asyncio
async def test_a_corrupt_compressed_body_is_a_failed_request_not_an_exception():
    handler = make_http_request_handler(http_client=_encoded_client(b"this is not gzip data at all", "gzip"), response_body_byte_cap=CAP)

    result = await handler({"url": "https://example.com/"})

    assert result.is_error and "failed" in result.output, result.output


@pytest.mark.asyncio
async def test_both_tools_ask_only_for_encodings_they_can_bound():
    seen: list[httpx.Request] = []
    await make_http_request_handler(http_client=_encoded_client(b"x", None, seen), response_body_byte_cap=CAP)({"url": "https://example.com/"})
    await LocalAdapter(client=_encoded_client(b"x", None, seen)).fetch(url="https://example.com/a.txt")
    await make_http_request_handler(http_client=_encoded_client(b"x", None, seen), response_body_byte_cap=CAP)(
        {"url": "https://example.com/", "headers": {"accept-encoding": "br"}},
    )

    assert [r.headers["accept-encoding"] for r in seen] == ["gzip, deflate", "gzip, deflate", "br"], (
        "the agent's own Accept-Encoding is its own; the default is the set the tools can bound"
    )


# ---- stacked encodings, redirects, download, the PDF branch, and the timeout log (second review of this PR) ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["http_request", "local_fetch", "download"])
async def test_stacked_encodings_are_refused_before_a_byte_is_decoded(which):
    """`Content-Encoding: gzip, gzip` makes httpx stack decoders: a body of a few KB inflates to gigabytes in ONE decode step. At most one
    layer is accepted; stacked or unknown codings are refused with an explicit allow-list, whatever the layers are."""
    stacked = _gzip(_gzip(b"\0" * (16 << 20)))
    client = _encoded_client(stacked, "gzip, gzip")
    tracemalloc.start()
    try:
        if which == "http_request":
            result = await make_http_request_handler(http_client=client, response_body_byte_cap=CAP)({"url": "https://example.com/"})
            assert result.is_error and "unsupported content-encoding" in result.output, result.output
        elif which == "local_fetch":
            with pytest.raises(WebFetchProviderError, match="unsupported content-encoding"):
                await LocalAdapter(client=client, raw_byte_cap=CAP).fetch(url="https://example.com/x.txt")
        else:
            result, _ = await _download(client, cap=CAP)
            assert result.is_error and "unsupported content-encoding" in result.output, result.output
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 4 * CAP, f"{peak} bytes were held to refuse a stacked body"


class _Ws:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []

    async def write_file(self, path: str, content: bytes) -> None:
        self.writes.append((path, content))


class _WsRegistry:
    def __init__(self) -> None:
        self.ws = _Ws()

    async def get_workspace(self, workspace_id: str):
        return self.ws


async def _download(client: httpx.AsyncClient, *, cap: int, arguments: dict | None = None):
    from primer.model.yield_ import ToolContext
    from primer.toolset.web.tools import make_download_handler

    registry = _WsRegistry()
    handler = make_download_handler(http_client=client, workspace_registry=registry, byte_cap=cap)
    ctx = ToolContext(tool_call_id="c1", session_id="s1", workspace_id="w1", initiated_by=None)
    result = await handler({"url": "https://example.com/file.bin", **(arguments or {})}, ctx=ctx)
    return result, registry.ws


@pytest.mark.asyncio
async def test_download_does_not_inflate_a_compression_bomb_past_its_cap():
    bomb = _bomb(64 << 20)
    tracemalloc.start()
    try:
        result, ws = await asyncio.wait_for(_download(_encoded_client(bomb, "gzip"), cap=CAP), 20)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.is_error and "exceeds the maximum" in result.output and ws.writes == [], (result.output, ws.writes)
    assert peak < 8 * CAP + (1 << 20), f"the decode step held {peak} bytes for a {CAP}-byte cap"


@pytest.mark.asyncio
async def test_download_writes_the_decoded_bytes_of_a_compressed_file():
    text = b"a downloadable file\n" * 100
    result, ws = await _download(_encoded_client(_gzip(text), "gzip"), cap=CAP)

    assert not result.is_error, result.output
    assert ws.writes == [("file.bin", text)] and json.loads(result.output)["bytes"] == len(text)


class _Redirects:
    """A site whose `/start` answers 302 -> `/end` with a body that never ends, and whose `/end` answers 200."""

    def __init__(self, hops: int = 1, body_kind: str = "endless") -> None:
        self.redirect_body = _Body()
        self.hops = hops
        self.requested: list[str] = []
        self._body_kind = body_kind

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requested.append(str(request.url))
            path = request.url.path
            if path.startswith("/hop"):
                n = int(path[4:])
                target = f"/hop{n + 1}" if n + 1 < self.hops else "/end"
                response = httpx.Response(302, headers={"location": target}, content=self.redirect_body.stream())
                self.redirect_body.response = response
                return response
            if path == "/loop":
                return httpx.Response(302, headers={"location": "/loop"})
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="the final page")

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_the_local_fetch_does_not_read_the_body_of_a_redirect():
    site = _Redirects()
    adapter = LocalAdapter(client=site.client(), raw_byte_cap=CAP)

    page = await asyncio.wait_for(adapter.fetch(url="https://example.com/hop0"), 10)

    assert page.content_markdown == "the final page" and page.final_url.endswith("/end")
    assert site.redirect_body.pulled <= 2 * CHUNK, f"{site.redirect_body.pulled} bytes of a redirect's body were read"
    assert site.redirect_body.closed, "the redirect response was left open"


@pytest.mark.asyncio
async def test_the_local_fetch_follows_a_bounded_number_of_hops():
    site = _Redirects(hops=30)
    adapter = LocalAdapter(client=site.client(), raw_byte_cap=CAP)

    with pytest.raises(WebFetchProviderError, match="too many redirects"):
        await asyncio.wait_for(adapter.fetch(url="https://example.com/hop0"), 10)

    assert len(site.requested) <= 11


@pytest.mark.asyncio
async def test_a_redirect_loop_is_a_failed_fetch_not_a_hang():
    adapter = LocalAdapter(client=_Redirects().client(), raw_byte_cap=CAP)

    with pytest.raises(WebFetchProviderError, match="too many redirects"):
        await asyncio.wait_for(adapter.fetch(url="https://example.com/loop"), 10)


@pytest.mark.asyncio
async def test_every_redirect_hop_still_passes_the_egress_guard(monkeypatch):
    """The hops are made by hand now, through the same guarded client: a hop to an internal address is refused and never connected to."""
    from tests.common.test_netguard import _client, _fake_dns, _redirect_to, _RecordingBackend

    _fake_dns(monkeypatch, {"public.example": ["93.184.216.34"]})
    backend = _RecordingBackend([_redirect_to("http://127.0.0.1/admin")])
    adapter = LocalAdapter(client=_client(backend))

    with pytest.raises(WebFetchProviderError):
        await adapter.fetch(url="http://public.example/start")

    assert [host for host, _ in backend.log["connect"]] == ["93.184.216.34"], "the loopback hop was connected to"


@pytest.mark.asyncio
async def test_a_pdf_is_refused_with_an_unsupported_content_error_not_a_name_error():
    """`_extract_pdf` named `UnsupportedContentError` without importing it, so every PDF fetch was a NameError."""
    from primer.model.except_ import UnsupportedContentError

    client = _encoded_client(b"%PDF-1.4 not extracted", None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4")

    adapter = LocalAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    with pytest.raises(UnsupportedContentError):
        await adapter.fetch(url="https://example.com/x.pdf")
    await client.aclose()


@pytest.mark.asyncio
async def test_the_http_request_timeout_is_logged_like_a_transport_failure(caplog):
    body = _Body(chunk=b"x", delay=0.05)
    handler = make_http_request_handler(http_client=_client(body), response_body_byte_cap=CAP)

    with caplog.at_level("WARNING", logger="primer.toolset.web.tools"):
        result = await handler({"url": "https://example.com/slow", "timeout_seconds": 0.2, "method": "GET"})

    assert result.is_error
    record = next((r for r in caplog.records if "timed out" in r.getMessage()), None)
    assert record is not None, [r.getMessage() for r in caplog.records]
    assert (record.url, record.method, record.timeout_seconds) == ("https://example.com/slow", "GET", 0.2)
