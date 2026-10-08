"""Read an HTTP response body only up to a byte cap (architecture review A-10).

``response.content`` reads the WHOLE body before anything can trim it, so a URL that streams gigabytes is held in memory. The tools that
fetch a URL on behalf of an agent open the response as a stream and read it through :func:`read_capped`, which stops at the cap and leaves
closing the response to the ``async with`` that owns it. The bytes are the DECODED ones (``aiter_bytes`` inflates a gzip or deflate body as it
goes), so the cap bounds a decompression bomb as well. A total-time bound is the caller's (``asyncio.timeout`` around the request AND this
read): the client's own timeouts are per operation, and a body that drips a byte at a time never trips them.
"""

from __future__ import annotations

from contextlib import aclosing

import httpx

__all__ = ["read_capped"]


async def read_capped(response: httpx.Response, cap: int) -> tuple[bytes, bool]:
    """Read at most ``cap`` bytes of the open, streamed ``response``; the flag says more bytes followed the cap."""
    data = bytearray()
    # ``aclosing``: leaving the loop at the cap closes the chunk iterator now, not when the garbage collector gets to it.
    async with aclosing(response.aiter_bytes()) as chunks:
        async for chunk in chunks:
            room = cap - len(data)
            if len(chunk) > room:
                data += chunk[:room]
                return bytes(data), True
            data += chunk
    return bytes(data), False
