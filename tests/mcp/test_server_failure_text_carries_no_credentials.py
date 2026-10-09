"""An exposed tool's failure text reaches an MCP client without credentials (security ticket 01a11fbc-d6de).

``_call`` returns ``str(exc)`` for a dispatch that raised and ``result.output`` for a result that is an error; both are whatever the library printed,
and httpx prints the request URL whole. A result that is not an error is data and is returned as it was.
"""

from __future__ import annotations

import pytest
from mcp.types import CallToolRequestParams

from primer.mcp.exposure import update_exposure
from primer.mcp.server import build_mcp_server
from primer.model.chat import ToolCallResult
from tests.mcp.test_server_handlers import _deps, _registry_with_schema

LEAKY = (
    "ConnectError: All connection attempts failed for url 'https://svc-user:hunter2pw@gateway.internal/v1/x?api_key=SKSECRET123456' "
    "(retry with Bearer sk-abcdefgh12345678 or Basic dXNlcjpwYXNzd29yZA==)"
)
SECRETS = ("hunter2pw", "SKSECRET123456", "sk-abcdefgh12345678", "dXNlcjpwYXNzd29yZA==")


async def _call(fake_storage_provider, monkeypatch, *, raises: BaseException | None = None, result: ToolCallResult | None = None):
    async def _invoke(**kwargs):
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr("primer.mcp.server.invoke_exposed", _invoke)
    registry = _registry_with_schema({"type": "object", "properties": {}})
    deps = _deps(fake_storage_provider, registry)
    await update_exposure(enabled=True, allowed_tools=["misc__strict"], updated_by="alice", deps=deps)
    handler = build_mcp_server(lambda: deps).get_request_handler("tools/call").handler
    return await handler(None, CallToolRequestParams(name="misc__strict", arguments={}))


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked in {text!r}"
    assert "gateway.internal" in text


@pytest.mark.asyncio
async def test_a_dispatch_that_raised_is_returned_without_the_credentials(fake_storage_provider, monkeypatch):
    out = await _call(fake_storage_provider, monkeypatch, raises=RuntimeError(LEAKY))

    assert out.is_error is True
    _assert_clean(out.content[0].text)


@pytest.mark.asyncio
async def test_an_error_result_is_returned_without_the_credentials(fake_storage_provider, monkeypatch):
    out = await _call(fake_storage_provider, monkeypatch, result=ToolCallResult(output=f"http-request failed: {LEAKY}", is_error=True))

    assert out.is_error is True
    _assert_clean(out.content[0].text)


@pytest.mark.asyncio
async def test_a_successful_result_and_a_clean_error_are_returned_as_they_were(fake_storage_provider, monkeypatch):
    ok = await _call(fake_storage_provider, monkeypatch, result=ToolCallResult(output=LEAKY, is_error=False))
    assert ok.content[0].text == LEAKY

    plain = "http-request failed: ConnectError for url 'https://example.com/v1/x?page=2'"
    bad = await _call(fake_storage_provider, monkeypatch, result=ToolCallResult(output=plain, is_error=True))
    assert bad.content[0].text == plain
