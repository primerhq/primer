"""Read an HTTP response body only up to a byte cap (architecture review A-10).

``response.content`` reads the WHOLE body before anything can trim it, so a URL that streams gigabytes is held in memory. The tools that
fetch a URL on behalf of an agent open the response as a stream and read it through :func:`read_capped`, which stops at the cap and leaves
closing the response to the ``async with`` that owns it.

The cap counts DECODED bytes and bounds every decode STEP, not only what is kept: httpx decodes each raw chunk whole, so one small gzip chunk
(64 MiB of zeros is about 64 KB on the wire) is inflated in a single step before anything can trim it. This module reads the RAW chunks
(``aiter_raw``) and inflates them itself with ``zlib``'s ``max_length``, so a step never produces more than the room left under the cap plus
one byte (that byte is how it knows more followed). It decodes ``gzip`` and ``deflate`` (zlib-wrapped, or raw as some servers send it) and
identity; any other ``Content-Encoding`` (``br``, ``zstd``, a chain) raises :class:`UnsupportedContentEncoding` before a byte is read, because
there is no decoder here whose output can be bounded, and :data:`ACCEPT_ENCODING` is what the tools send so that an honest server does not
choose one. A total-time bound is the caller's (``asyncio.timeout`` around the request AND this read): the client's own timeouts are per
operation, and a body that drips a byte at a time never trips them.
"""

from __future__ import annotations

import zlib
from contextlib import aclosing

import httpx

__all__ = ["ACCEPT_ENCODING", "UnsupportedContentEncoding", "read_capped"]

# The codings :func:`read_capped` can decode with a bound on each step; what the tools ask for unless the caller chose its own.
ACCEPT_ENCODING = "gzip, deflate"


class UnsupportedContentEncoding(Exception):
    """The response is encoded with something this module cannot decode within a bound."""

    def __init__(self, encoding: str) -> None:
        super().__init__(f"unsupported content-encoding {encoding!r}; the tools read gzip, deflate or none")
        self.encoding = encoding


class _Identity:
    def step(self, data: bytes, limit: int) -> tuple[bytes, bytes]:
        return data[:limit], data[limit:]


class _Inflate:
    """A ``zlib`` decoder whose every step yields at most ``limit`` bytes; ``raw_fallback`` retries a deflate body that is not zlib-wrapped."""

    def __init__(self, wbits: int, *, raw_fallback: bool = False) -> None:
        self._decoder = zlib.decompressobj(wbits)
        self._raw_fallback = raw_fallback
        self._started = False

    def step(self, data: bytes, limit: int) -> tuple[bytes, bytes]:
        try:
            out = self._decoder.decompress(data, limit)
        except zlib.error as exc:
            if self._raw_fallback and not self._started:
                self._raw_fallback = False
                self._decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                return self.step(data, limit)
            raise httpx.DecodingError(f"could not decode the response body: {exc}") from exc
        self._started = True
        return out, self._decoder.unconsumed_tail


def _decoder_for(content_encoding: str) -> _Identity | _Inflate:
    coding = content_encoding.strip().lower()
    if coding in ("", "identity"):
        return _Identity()
    if coding in ("gzip", "x-gzip"):
        return _Inflate(16 + zlib.MAX_WBITS)
    if coding == "deflate":
        return _Inflate(zlib.MAX_WBITS, raw_fallback=True)
    raise UnsupportedContentEncoding(content_encoding)


async def read_capped(response: httpx.Response, cap: int) -> tuple[bytes, bool]:
    """Read at most ``cap`` DECODED bytes of the open, streamed ``response``; the flag says more bytes followed the cap.

    Raises :class:`UnsupportedContentEncoding` for an encoding it cannot bound and ``httpx.DecodingError`` for a body that does not decode.
    """
    if response.is_stream_consumed:
        # Already read AND decoded by whoever built the response (a transport that buffers, a test double built with ``content=``): there is no
        # stream left to bound, and the bytes are in memory. Apply the cap to them.
        body = response.content
        return body[:cap], len(body) > cap
    decoder = _decoder_for(response.headers.get("content-encoding", ""))
    data = bytearray()
    # ``aclosing``: leaving the loop at the cap closes the chunk iterator now, not when the garbage collector gets to it.
    async with aclosing(response.aiter_raw()) as chunks:
        async for raw in chunks:
            room = cap - len(data)
            out, _unconsumed = decoder.step(raw, room + 1)
            if len(out) > room:
                data += out[:room]
                return bytes(data), True
            data += out
    return bytes(data), False
