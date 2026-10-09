"""An MCP toolset probe never echoes the toolset's configured header secret (security ticket 01a11eda-2031, #672 review round 1, B1).

A header value pasted with a trailing newline, space or tab is not a valid header value, and h11 refuses it before the request leaves
(``LocalProtocolError: Illegal header value b'Bearer sk-live-...\\n'``). ``classify_mcp_exception`` turned that into a ``ProviderError`` whose text held the
whole value, and four surfaces served it: ``GET /v1/toolsets/{id}/tools`` (the 502 detail), ``GET /v1/tools`` (``unavailable_reason``, served to EVERY user
who opens the agent picker), the ``list_toolset_tools`` system tool (the agent's transcript, and so the model vendor) and the catalogue's log. These tests use
a REAL ``McpToolsetProvider`` built by the registry from the stored toolset, over a loopback port that accepts and hangs up (nothing leaves the machine).
"""

from __future__ import annotations

import logging

import pytest

from primer.model.except_ import PrimerError
from primer.toolset._system_crud import _list_toolset_tools_tool
from tests.api.test_probe_errors_do_not_echo_the_key import KEY, TAIL, port  # noqa: F401

PADDINGS = ["\n", " ", "\t", "\r\n"]


@pytest.fixture(autouse=True)
def _real_mcp_registry(app):
    """The API conftest's provider registry builds ``object()`` for every toolset, which has no ``list_tools`` (a first version of these tests failed on that
    and asserted nothing about the key). Swap in a registry with the REAL toolset factory, so the routes drive a real ``McpToolsetProvider`` over real httpx."""
    from primer.api.registries.provider_registry import ProviderRegistry, _build_default_toolset_factory

    app.state.provider_registry = ProviderRegistry(app.state.storage_provider, toolset_factory=_build_default_toolset_factory())


def _clean(text: str) -> bool:
    return KEY not in text and TAIL not in text


async def _create_toolset(client, port: int, tid: str, header_value: str, header: str = "Authorization") -> None:
    r = await client.post(
        "/v1/toolsets?allow_unreachable=true",
        json={
            "id": tid,
            "provider": "mcp",
            "config": {"transport": "http", "config": {"url": f"http://127.0.0.1:{port}/mcp", "headers": {header: header_value}}},
        },
    )
    assert r.status_code in (200, 201), r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("padding", PADDINGS, ids=["newline", "space", "tab", "crlf"])
async def test_the_toolset_tools_route_does_not_echo_a_padded_header_secret(client, port, padding) -> None:
    await _create_toolset(client, port, "ts-pad", f"Bearer {KEY}{padding}")

    r = await client.get("/v1/toolsets/ts-pad/tools")

    assert r.status_code == 502, r.text
    assert _clean(r.text), f"the header secret leaked in {r.text!r}"
    assert "Illegal header value" in r.text, "what failed must still be said"


@pytest.mark.asyncio
async def test_a_custom_header_secret_with_a_padding_is_masked_too(client, port) -> None:
    await _create_toolset(client, port, "ts-xkey", KEY + "\n", header="X-Api-Key")

    r = await client.get("/v1/toolsets/ts-xkey/tools")

    assert r.status_code == 502, r.text
    assert _clean(r.text)


@pytest.mark.asyncio
async def test_the_catalogue_does_not_serve_the_secret_as_the_unavailable_reason(client, port, caplog) -> None:
    """``GET /v1/tools`` is served to every user who opens the agent picker."""
    await _create_toolset(client, port, "ts-cat", f"Bearer {KEY}\n")

    # INFO and above: what a deployment emits. The HTTP library's own DEBUG trace (``httpcore2.http11``) repeats the refused header value in its
    # ``send_request_headers.failed`` line; primer does not turn that on, and it is the log filter's job (ticket 01a1201c-8918), not the catalogue's.
    with caplog.at_level(logging.INFO):
        r = await client.get("/v1/tools")

    assert r.status_code == 200, r.text
    assert _clean(r.text), "the catalogue served the header secret"
    entry = next(t for t in r.json()["items"] if t["id"] == "ts-cat")
    assert entry["available"] is False and entry["unavailable_reason"]
    leaked = [(rec.name, rec.levelname) for rec in caplog.records if not _clean(rec.getMessage())]
    assert not leaked, f"the catalogue logged the header secret: {leaked}"


@pytest.mark.asyncio
async def test_the_list_toolset_tools_system_tool_does_not_hand_the_secret_to_the_agent(client, app, port) -> None:
    await _create_toolset(client, port, "ts-sys", f"Bearer {KEY}\n")
    _name, (_tool, handler) = _list_toolset_tools_tool(app.state.provider_registry)

    with pytest.raises(PrimerError) as raised:
        await handler({"toolset_id": "ts-sys"})

    assert _clean(str(raised.value)) and _clean(raised.value.message)


@pytest.mark.asyncio
async def test_an_error_that_carries_no_header_secret_is_reported_as_it_was(client) -> None:
    """Nothing listens on port 9: a refused connection says so, and the words are not touched."""
    r = await client.post(
        "/v1/toolsets?allow_unreachable=true",
        json={"id": "ts-clean", "provider": "mcp", "config": {"transport": "http", "config": {"url": "http://127.0.0.1:9/mcp", "headers": {"Authorization": f"Bearer {KEY}"}}}},
    )
    assert r.status_code in (200, 201), r.text

    got = await client.get("/v1/toolsets/ts-clean/tools")

    assert got.status_code == 504, got.text      # a refused connection is a NetworkError (504), not the ProviderError (502) of a refused header
    assert _clean(got.text) and "could not connect to the MCP server" in got.text
