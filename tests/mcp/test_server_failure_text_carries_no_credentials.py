"""An exposed tool's failure text reaches an MCP client without credentials (security ticket 01a11fbc-d6de).

A raised dispatch is mapped to the fixed bounded ``dispatch_failed`` text
(``Tool dispatch failed.``; the exception detail stays server-side in the
operator log), and an error result to a ``tool_failed`` envelope whose
message is scrubbed through ``redact_credentials``; httpx prints the request
URL whole, so the redaction is what keeps the userinfo and keys off the
wire. A successful result's output is data and travels unredacted inside
the ``result_text`` envelope.
"""

from __future__ import annotations

import json

import pytest
from mcp.types import CallToolRequestParams

from primer.mcp.exposure import update_exposure
from primer.mcp.server import build_mcp_server, current_actor
from primer.model.chat import ToolCallResult
from primer.model.principal import Principal
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

    # The meta wire resolves the inner target through the authorized
    # catalogue before dispatch; patching the meta module's name
    # intercepts the inner invoke_exposed, and an admin actor clears the
    # catalogue's role floor for the fixture tool.
    monkeypatch.setattr("primer.mcp.meta_tools.invoke_exposed", _invoke)
    registry = _registry_with_schema({"type": "object", "properties": {}})
    deps = _deps(fake_storage_provider, registry)
    await update_exposure(enabled=True, allowed_tools=["misc__strict"], updated_by="alice", deps=deps)
    actor_token = current_actor.set(
        Principal(type="user", id="u", display="u", role="admin", source="local")
    )
    try:
        handler = build_mcp_server(lambda: deps).get_request_handler("tools/call").handler
        return await handler(None, CallToolRequestParams(
            name="call_tool",
            arguments={"scoped_id": "misc__strict", "arguments": {}},
        ))
    finally:
        current_actor.reset(actor_token)


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked in {text!r}"
    assert "gateway.internal" in text


@pytest.mark.asyncio
async def test_a_dispatch_that_raised_is_returned_without_the_credentials(fake_storage_provider, monkeypatch):
    out = await _call(fake_storage_provider, monkeypatch, raises=RuntimeError(LEAKY))

    # The wire carries one fixed bounded message; the exception detail
    # (with its credentials) is server-side only.
    assert out.is_error is True
    assert out.content[0].text == "Tool dispatch failed."
    for secret in SECRETS:
        assert secret not in out.content[0].text


@pytest.mark.asyncio
async def test_an_error_result_is_returned_without_the_credentials(fake_storage_provider, monkeypatch):
    out = await _call(fake_storage_provider, monkeypatch, result=ToolCallResult(output=f"http-request failed: {LEAKY}", is_error=True))

    assert out.is_error is True
    _assert_clean(out.content[0].text)


@pytest.mark.asyncio
async def test_a_successful_result_is_enveloped_and_a_clean_error_keeps_its_words(
    fake_storage_provider, monkeypatch,
):
    ok = await _call(fake_storage_provider, monkeypatch, result=ToolCallResult(output=LEAKY, is_error=False))
    assert json.loads(ok.content[0].text) == {"ok": True, "output": LEAKY}

    plain = "http-request failed: ConnectError for url 'https://example.com/v1/x?page=2'"
    bad = await _call(fake_storage_provider, monkeypatch, result=ToolCallResult(output=plain, is_error=True))
    payload = json.loads(bad.content[0].text)
    assert payload == {"ok": False, "error": {"code": "tool_failed", "message": plain}}
