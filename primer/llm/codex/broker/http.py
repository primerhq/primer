"""Fixed-origin TLS transport. No proxy/env/redirect or arbitrary RPC escape."""
from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx

from primer.llm.codex.boundary import BoundaryError
from primer.llm.codex.request import decode
from .security import require

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
MODELS = RESOURCE + "/models"
RESPONSES = RESOURCE + "/responses"
MAX_EVENT_BYTES = 1048576


class FixedHTTP:
    def __init__(self, *, _test_transport=None):
        # The test transport is a trusted code dependency, never protocol data.
        self._client = httpx.AsyncClient(trust_env=False, follow_redirects=False, verify=True,
                                        headers={"Accept-Encoding": "identity"},
                                        timeout=httpx.Timeout(30, connect=10),
                                        limits=httpx.Limits(max_connections=8), transport=_test_transport)

    @staticmethod
    async def _raw(response: httpx.Response, budget: int):
        # Refuse BEFORE iterating: aiter_bytes decodes before application limits.
        # chunk_size there cannot bound decompressor output or CPU consumption.
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        require(encoding == "identity")
        length = response.headers.get("content-length")
        if length is not None:
            require(length.isascii() and length.isdigit() and len(length) <= 12 and int(length) <= budget)
        used = 0
        async for chunk in response.aiter_raw():
            require(len(chunk) <= budget - used)
            used += len(chunk)
            yield chunk

    async def json(self, url: str, *, form: dict | None = None, bearer: str | None = None) -> dict:
        allowed = {DISCOVERY, TOKEN, MODELS}
        require(url in allowed or self._auth_metadata_url(url))
        require((url == TOKEN) == (form is not None))
        require((url == MODELS) == (bearer is not None))
        try:
            async with self._client.stream("POST" if form is not None else "GET", url, data=form,
                                           headers={"Authorization": "Bearer " + bearer} if bearer else {}) as response:
                require(response.status_code == 200)
                raw = bytearray()
                async for chunk in self._raw(response, 262144):
                    raw.extend(chunk)
                result = decode(bytes(raw))
                require(type(result) is dict)
                return result
        except Exception:
            raise BoundaryError("Broker upstream request refused or failed") from None

    @staticmethod
    def _auth_metadata_url(url: str) -> bool:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.netloc == "auth.openai.com"
                and parsed.path.startswith("/.well-known/") and not parsed.query and not parsed.fragment)

    async def events(self, request: dict, bearer: str) -> AsyncIterator[dict]:
        # Only broker-validated wire reaches this method; no endpoint argument.
        try:
            async with self._client.stream("POST", RESPONSES, json=request,
                                           headers={"Authorization": "Bearer " + bearer,
                                                    "Accept": "text/event-stream"}) as response:
                require(response.status_code == 200 and response.headers.get("content-type", "").split(";")[0] == "text/event-stream")
                buffer = bytearray()
                data = bytearray()
                has_data = False
                async for chunk in self._raw(response, 33554432):
                    require(len(chunk) <= MAX_EVENT_BYTES - len(buffer))
                    scan = len(buffer)  # Existing partial line was already scanned.
                    buffer.extend(chunk)
                    start = 0
                    while (end := buffer.find(b"\n", scan)) >= 0:
                        line = buffer[start:end].rstrip(b"\r")
                        start = scan = end + 1
                        if not line:
                            if has_data:
                                raw = bytes(data)
                                data.clear()
                                has_data = False
                                if raw == b"[DONE]":
                                    return
                                event = decode(raw)
                                require(type(event) is dict)
                                yield event
                        elif line.startswith(b"data:"):
                            part = line[5:].lstrip(b" ")
                            # Count separators too: empty data lines still grow
                            # an event. No per-line scan or unbounded entry list.
                            extra = len(part) + int(has_data)
                            require(extra <= MAX_EVENT_BYTES - len(data))
                            if has_data:
                                data.extend(b"\n")
                            data.extend(part)
                            has_data = True
                        elif not (line.startswith(b":") or line.startswith(b"event:")):
                            raise BoundaryError("Unsupported stream framing")
                    # Drop consumed bytes once per chunk, not once per line.
                    del buffer[:start]
                require(not has_data and not buffer)
        except Exception:
            raise BoundaryError("Broker model stream failed") from None

    async def revoke(self, endpoint: str, client: str, refresh: str) -> None:
        parsed = urlsplit(endpoint)
        require(parsed.scheme == "https" and parsed.netloc == "auth.openai.com"
                and parsed.path.startswith("/api/accounts/") and not parsed.query and not parsed.fragment)
        try:
            async with self._client.stream("POST", endpoint, data={"token": refresh,
                    "token_type_hint": "refresh_token", "client_id": client}) as response:
                require(response.status_code == 200)
        except Exception:
            raise BoundaryError("SIWC revocation failed; local credentials remain cleared") from None

    async def close(self) -> None:
        await self._client.aclose()
