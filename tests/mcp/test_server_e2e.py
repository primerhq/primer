"""End-to-end SDK e2e — Spec §14.

Drives the lowlevel :class:`Server` returned by
:func:`primer.mcp.server.build_mcp_server` through the MCP SDK's
in-memory transport. This exercises the full request/response loop —
JSON-RPC framing, ``initialize`` handshake, ``tools/list`` +
``tools/call`` dispatch — without spinning up an HTTP server.

The unit-level handler tests in ``test_server_handlers.py`` already
pin down the dispatch + audit + error-mapping contract; this file
proves the SDK glue is correct: handler return shapes round-trip
through the protocol, the allowlist filters ``tools/list``, and
calling a disallowed tool surfaces as a JSON-RPC error (mapped to
``MCPError`` on the client) rather than a successful response.

``_connected_client_session`` wires client + server with a pair of
in-memory anyio streams and runs the server in a background task; the
test body sees a ready-to-use ``ClientSession`` with ``initialize()``
already called. mcp>=2.0 dropped the SDK's own all-in-one
``mcp.shared.memory.create_connected_server_and_client_session``
helper in favour of the lower-level ``create_client_server_memory_streams``
primitive; ``mcp.client._memory.InMemoryTransport`` (private module,
used by the SDK's own test suite for exactly this purpose) wraps that
primitive with the server-lifecycle/shutdown handling this helper
would otherwise have to reimplement.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.lowlevel import Server

from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.server import build_mcp_server, current_actor
from primer.model.principal import Principal


def _deps(storage, registry) -> ExposureDeps:
    return ExposureDeps(storage_provider=storage, provider_registry=registry)


@asynccontextmanager
async def _connected_client_session(server: Server) -> AsyncIterator[ClientSession]:
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


_SAFE = "misc__uuid_v4"


@pytest.mark.asyncio
async def test_list_returns_only_allowed(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """The wire advertises exactly the three meta-tools; the allowlist
    narrowing is honoured by the ``list_tools`` meta end-to-end.

    Two safe tools exist in the fake registry (``uuid_v4`` + ``now``).
    Allowlisting only ``uuid_v4`` (allowlist mode) must filter ``now``
    out of the discovery answer - proving the discovery layer's
    narrowing is honoured end-to-end and not bypassed by the SDK.
    """
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    await update_exposure(
        enabled=True, mode="allowlist", allowed_tools=[_SAFE],
        updated_by="alice", deps=deps,
    )
    server = build_mcp_server(lambda: deps)

    actor_tok = current_actor.set(
        Principal(
            type="system", id="test-system", display="test-system",
            role=None, source="system",
        )
    )
    try:
        async with _connected_client_session(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            listed = await client.call_tool("list_tools", arguments={"limit": 50})
    finally:
        current_actor.reset(actor_tok)

    assert names == {"list_tools", "find_tools", "call_tool"}
    payload = json.loads(listed.content[0].text)
    assert [item["scoped_id"] for item in payload["items"]] == [_SAFE]


@pytest.mark.asyncio
async def test_call_allowed_returns_result(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """``call_tool`` on an allowed tool returns the provider's output.

    The fake provider's default handler echoes ``f"{name}:{arguments}"``
    so the round-trip can be asserted exactly through the ``result_text``
    envelope. ``isError`` must be ``False``.

    Sets ``current_actor`` to a system-type Principal (the auth-disabled
    bypass) before the server task is spawned so it clears the RBAC
    floor - this test is about the SDK round-trip, not RBAC.
    """
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    await update_exposure(
        enabled=True, allowed_tools=[_SAFE],
        updated_by="alice", deps=deps,
    )
    server = build_mcp_server(lambda: deps)

    actor_tok = current_actor.set(
        Principal(
            type="system", id="test-system", display="test-system",
            role=None, source="system",
        )
    )
    try:
        async with _connected_client_session(server) as client:
            result = await client.call_tool(
                "call_tool",
                arguments={"scoped_id": _SAFE, "arguments": {"foo": "bar"}},
            )
    finally:
        current_actor.reset(actor_tok)

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    # Echo from FakeToolsetProvider.call default handler, inside the envelope.
    assert "uuid_v4" in payload["output"]
    assert "foo" in payload["output"]


@pytest.mark.asyncio
async def test_call_disallowed_returns_not_exposed(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """Calling outside the allowlist surfaces as a JSON-RPC error.

    The server raises :class:`MCPError` with code ``METHOD_NOT_FOUND``;
    the SDK's client-side dispatch either re-raises it as
    :class:`MCPError` or returns ``isError=True``. Both are valid
    failure shapes per the SDK contract — accept either, and confirm the
    response is *not* a successful tool result.
    """
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    # Enabled but nothing exposed — the safe tool is denied.
    await update_exposure(
        enabled=True, allowed_tools=[],
        updated_by="x", deps=deps,
    )
    server = build_mcp_server(lambda: deps)

    async with _connected_client_session(server) as client:
        try:
            result = await client.call_tool(_SAFE, arguments={})
        except Exception as exc:  # noqa: BLE001 -- protocol error path
            msg = str(exc).lower()
            assert (
                "tool is unavailable" in msg
                or "not exposed" in msg
                or "method" in msg
                or "not_exposed" in msg
            ), f"unexpected error shape: {exc!r}"
            return
        # Some SDK versions surface protocol errors as isError=True
        # rather than raising — accept that too.
        assert result.is_error is True


@pytest.mark.asyncio
async def test_disabled_exposure_returns_empty_catalogue(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """A disabled exposure leaves the three meta-tools advertised but
    empties the caller-visible catalogue: discovery is the gate.

    The exposure singleton is lazy-created with ``enabled=False`` on
    first read, so the test skips the explicit ``update_exposure`` to
    prove the default state is safe.
    """
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    server = build_mcp_server(lambda: deps)

    async with _connected_client_session(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        listed = await client.call_tool("list_tools", arguments={})

    assert names == {"list_tools", "find_tools", "call_tool"}
    payload = json.loads(listed.content[0].text)
    assert payload["items"] == []


# ---- Fixed, bounded, credential-safe outer errors (seam spec §4) -----------
#
# The wire text of the OUTER failure paths is fixed and input-free: an
# unknown name answers method-not-found with the same message no matter
# what was requested, outer validation fails with the fixed marker, and a
# raised dispatch never carries the exception detail to the client.


@pytest.mark.asyncio
async def test_unknown_tool_name_is_fixed_method_not_found(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """A 5000-char name yields the fixed message and is never echoed."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    await update_exposure(
        enabled=True, allowed_tools=[_SAFE],
        updated_by="alice", deps=deps,
    )
    server = build_mcp_server(lambda: deps)

    actor_tok = current_actor.set(
        Principal(
            type="system", id="test-system", display="test-system",
            role=None, source="system",
        )
    )
    try:
        async with _connected_client_session(server) as client:
            try:
                result = await client.call_tool("x" * 5000, arguments={})
            except Exception as exc:  # noqa: BLE001 -- protocol error path
                msg = str(exc)
                assert "tool is unavailable" in msg.lower()
                assert "x" * 20 not in msg
                return
            assert result.is_error is True
    finally:
        current_actor.reset(actor_tok)


@pytest.mark.asyncio
async def test_outer_validation_is_bounded_and_input_free(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """A 10000-char scoped_id fails with the fixed marker, no echo."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    await update_exposure(
        enabled=True, allowed_tools=[_SAFE],
        updated_by="alice", deps=deps,
    )
    server = build_mcp_server(lambda: deps)

    actor_tok = current_actor.set(
        Principal(
            type="system", id="test-system", display="test-system",
            role=None, source="system",
        )
    )
    try:
        async with _connected_client_session(server) as client:
            result = await client.call_tool(
                "call_tool", arguments={"scoped_id": "a" * 10000},
            )
    finally:
        current_actor.reset(actor_tok)

    assert result.is_error is True
    text = result.content[0].text
    assert text == "Input validation error: arguments"
    assert len(text) <= 100
    assert "a" * 50 not in text


@pytest.mark.asyncio
async def test_dispatch_failure_is_fixed_and_clean(
    fake_storage_provider, fake_provider_registry_with_tools, monkeypatch, caplog,
) -> None:
    """A raised dispatch answers fixed text; the detail stays in the log."""
    import logging

    import primer.mcp.server as server_module

    async def _raise_meta(*_args, **_kwargs):
        raise RuntimeError("boom token=sk-secret123")

    monkeypatch.setattr(server_module, "invoke_meta_tool", _raise_meta)
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    await update_exposure(
        enabled=True, allowed_tools=[_SAFE],
        updated_by="alice", deps=deps,
    )
    server = build_mcp_server(lambda: deps)
    caplog.set_level(logging.ERROR, logger="primer.mcp.server")

    actor_tok = current_actor.set(
        Principal(
            type="system", id="test-system", display="test-system",
            role=None, source="system",
        )
    )
    try:
        async with _connected_client_session(server) as client:
            result = await client.call_tool("list_tools", arguments={})
    finally:
        current_actor.reset(actor_tok)

    assert result.is_error is True
    text = result.content[0].text
    assert text == "Tool dispatch failed."
    assert "boom" not in text
    assert "sk-secret123" not in text
    # Server-side: the operator sees the exception with its detail.
    logged = [r for r in caplog.records if r.name == "primer.mcp.server"]
    assert any("mcp: dispatch raised" in r.getMessage() for r in logged)
