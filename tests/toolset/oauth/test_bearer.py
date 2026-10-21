from __future__ import annotations

from types import SimpleNamespace

import httpx2

from primer.model.except_ import ProviderError
from primer.model.mcp_oauth import McpSignInRequiredError
from primer.toolset.oauth.bearer import McpBearerAuth


class _Service:
    def __init__(self, answer: str | Exception = "tok-2") -> None:
        self.answer = answer
        self.calls: list[tuple[str, str | None]] = []

    async def refresh_after_401(self, target, *, rejected: str, www_authenticate: str | None) -> str:
        self.calls.append((rejected, www_authenticate))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def test_the_token_goes_on_every_request() -> None:
    seen: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["Authorization"])
        return httpx2.Response(200)

    auth = McpBearerAuth(_Service(), SimpleNamespace(toolset_id="kb"), "tok-1")
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler), auth=auth) as client:
        await client.get("https://mcp.example/one")
        await client.get("https://mcp.example/two")
    assert seen == ["Bearer tok-1", "Bearer tok-1"]


async def test_a_401_refreshes_once_and_retries() -> None:
    seen: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["Authorization"])
        if len(seen) == 1:
            return httpx2.Response(401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
        return httpx2.Response(200)

    service = _Service()
    auth = McpBearerAuth(service, SimpleNamespace(toolset_id="kb"), "tok-1")
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler), auth=auth) as client:
        response = await client.get("https://mcp.example/mcp")
    assert response.status_code == 200
    assert seen == ["Bearer tok-1", "Bearer tok-2"]
    assert service.calls == [("tok-1", 'Bearer error="invalid_token"')]


async def test_a_failed_refresh_records_sign_in_and_returns_the_401() -> None:
    error = McpSignInRequiredError("kb", reason="gone")
    auth = McpBearerAuth(_Service(error), SimpleNamespace(toolset_id="kb"), "tok-1")
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda request: httpx2.Response(401)), auth=auth) as client:
        response = await client.get("https://mcp.example/mcp")
    assert response.status_code == 401
    assert auth.sign_in_required == str(error)


async def test_a_second_401_after_refresh_records_sign_in() -> None:
    auth = McpBearerAuth(_Service(), SimpleNamespace(toolset_id="kb"), "tok-1")
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda request: httpx2.Response(401)), auth=auth) as client:
        response = await client.get("https://mcp.example/mcp")
    assert response.status_code == 401
    assert auth.sign_in_required == "the MCP server kb needs an operator to reconnect it"


async def test_other_refresh_failures_are_recorded_not_raised() -> None:
    error = ProviderError("token endpoint down")
    auth = McpBearerAuth(_Service(error), SimpleNamespace(toolset_id="kb"), "tok-1")
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda request: httpx2.Response(401)), auth=auth) as client:
        response = await client.get("https://mcp.example/mcp")
    assert response.status_code == 401
    assert auth.failure is error
