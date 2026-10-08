"""Read an HTTP response body only up to a byte cap (architecture review A-10).

``response.content`` reads the WHOLE body before anything can trim it, so a URL that streams gigabytes is held in memory. The tools that
fetch a URL on behalf of an agent open the response as a stream and read it through :func:`read_capped`, which stops at the cap and leaves
closing the response to the ``async with`` that owns it.

The cap counts DECODED bytes and bounds every decode STEP, not only what is kept: httpx decodes each raw chunk whole, so one small gzip chunk
(64 MiB of zeros is about 64 KB on the wire) is inflated in a single step before anything can trim it. This module reads the RAW chunks
(``aiter_raw``) and inflates them itself with ``zlib``'s ``max_length``, so a step never produces more than the room left under the cap plus
one byte (that byte is how it knows more followed). It decodes ``gzip`` and ``deflate`` (zlib-wrapped, or raw as some servers send it) and
identity; any other ``Content-Encoding`` (``br``, ``zstd``, a chain) raises :class:`UnsupportedContentEncoding` as soon as the first chunk of a
non-empty body arrives (a HEAD reply, a 204 or a 304 carries the header and no body, and is not refused), because there is no decoder here whose
output can be bounded, and :data:`ACCEPT_ENCODING` is what the tools send so that an honest server does not choose one.

What else is bounded, and what is not:

* A compressed stream is decoded to its end and no further: bytes after the end of the stream (trailing garbage, a second gzip member) raise
  ``httpx.DecodingError`` and a finished decoder is never fed again (zlib keeps everything after the end in ``unused_data``, re-copied on each call,
  so feeding it was unbounded memory and quadratic time). A body that ends before the end of its stream (a dropped connection under
  connection-close framing) raises too, rather than reading as a complete body.
* The RAW bytes read are capped as well, a little over the decoded cap (:func:`_raw_ceiling`): a body can decode to nothing for as long as the
  server sends (a gzip header whose file name never ends), which the decoded cap never sees. An honest body is no larger on the wire than the
  bytes it decodes to, give or take framing.
* Time is the caller's (``asyncio.timeout`` around the request AND this read): the client's own timeouts are per operation, and a body that drips a
  byte at a time never trips them. ``download`` has no total deadline today; its raw ceiling bounds the work, not the clock.
"""

from __future__ import annotations

import zlib
from contextlib import aclosing

import httpx

__all__ = ["ACCEPT_ENCODING", "UnsupportedContentEncoding", "read_capped"]

# The codings :func:`read_capped` can decode with a bound on each step; what the tools ask for unless the caller chose its own.
ACCEPT_ENCODING = "gzip, deflate"

_AFTER_END = "data after the end of the compressed body"
_BEFORE_END = "the compressed body ends before its end-of-stream marker"
_OVER_CEILING = "the compressed body is larger than the cap allows"


def _raw_ceiling(cap: int) -> int:
    """Most RAW bytes read for a decoded ``cap``: the cap, an eighth more, and 64 KiB (framing and incompressible data cost far less than that)."""
    return cap + (cap >> 3) + (64 << 10)


class UnsupportedContentEncoding(Exception):
    """The response is encoded with something this module cannot decode within a bound."""

    def __init__(self, encoding: str) -> None:
        super().__init__(f"unsupported content-encoding {encoding!r}; the tools read gzip, deflate or none")
        self.encoding = encoding


class _Identity:
    def step(self, data: bytes, limit: int) -> tuple[bytes, bytes]:
        return data[:limit], data[limit:]

    def finish(self) -> None:
        return None


class _Inflate:
    """A ``zlib`` decoder whose every step yields at most ``limit`` bytes; ``raw_fallback`` retries a deflate body that is not zlib-wrapped."""

    def __init__(self, wbits: int, *, raw_fallback: bool = False) -> None:
        self._decoder = zlib.decompressobj(wbits)
        self._raw_fallback = raw_fallback
        self._started = False
        self._fed = False

    def step(self, data: bytes, limit: int) -> tuple[bytes, bytes]:
        if self._decoder.eof:
            # A finished decoder files everything it is given in ``unused_data`` (copied again on every call) and returns nothing.
            raise httpx.DecodingError(_AFTER_END)
        self._fed = True
        try:
            out = self._decoder.decompress(data, limit)
        except zlib.error as exc:
            if self._raw_fallback and not self._started:
                self._raw_fallback = False
                self._decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                return self.step(data, limit)
            raise httpx.DecodingError(f"could not decode the response body: {exc}") from exc
        if self._decoder.eof and self._decoder.unused_data:
            raise httpx.DecodingError(_AFTER_END)
        self._started = True
        return out, self._decoder.unconsumed_tail

    def finish(self) -> None:
        """The body ended: it must have ended at the end of the stream (``flush()`` is not called: it has no ``max_length``)."""
        if self._fed and not self._decoder.eof:
            raise httpx.DecodingError(_BEFORE_END)


def _decoder_for(content_encoding: str) -> _Identity | _Inflate:
    coding = content_encoding.strip().lower()
    if coding in ("", "identity"):
        return _Identity()
    if coding in ("gzip", "x-gzip"):
        return _Inflate(16 + zlib.MAX_WBITS)
    if coding == "deflate":
        return _Inflate(zlib.MAX_WBITS, raw_fallback=True)
    raise UnsupportedContentEncoding(content_encoding)


async def read_capped(response: httpx.Response, cap: int) -> tuple[bytearray, bool]:
    """Read at most ``cap`` DECODED bytes of the open, streamed ``response``; the flag says more bytes followed the cap.

    Returns a ``bytearray`` (a bytes-like: ``.decode()``, ``write_bytes`` and ``b64encode`` take it) so that the body is not copied a second time
    on the way out. Raises :class:`UnsupportedContentEncoding` for an encoding it cannot bound and ``httpx.DecodingError`` for a body that does not
    decode, has data after (or ends before) the end of its compressed stream, or is larger on the wire than its cap allows.

    A response whose stream was already consumed (see below) is a TEST DOUBLE or a transport that buffers: production streams, which are what these
    tools get from the network, never reach that branch.
    """
    if response.is_stream_consumed:
        # Already read AND decoded, whole and unbounded, by whoever built the response (a test double built with ``content=``, a transport that
        # buffers): there is no stream left to bound, and the bytes are in memory. Apply the cap to them.
        body = response.content
        return bytearray(body[:cap]), len(body) > cap
    decoder: _Identity | _Inflate | None = None
    ceiling = _raw_ceiling(cap)
    raw_total = 0
    data = bytearray()
    # ``aclosing``: leaving the loop at the cap closes the chunk iterator now, not when the garbage collector gets to it.
    async with aclosing(response.aiter_raw()) as chunks:
        async for raw in chunks:
            if not raw:
                continue
            if decoder is None:
                # Built at the first byte of a body, not before: a HEAD reply, a 204 or a 304 names the entity's coding and sends no body.
                decoder = _decoder_for(response.headers.get("content-encoding", ""))
            raw_total += len(raw)
            if raw_total > ceiling:
                raise httpx.DecodingError(_OVER_CEILING)
            room = cap - len(data)
            out, _unconsumed = decoder.step(raw, room + 1)
            if len(out) > room:
                data += out[:room]
                return data, True
            data += out
    if decoder is not None:
        decoder.finish()
    return data, False
