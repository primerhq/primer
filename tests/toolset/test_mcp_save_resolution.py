"""A non-admin's save of an MCP URL whose NAME resolves to an internal address is refused (W25 follow-up).

``toolset_admin_reason`` refuses an internal address written out in the URL. A FULL grantee of an ADMIN-owned toolset could still repoint it at a hostname
that resolves internal: the toolset's author is an admin, so ``author_is_trusted`` trusts it and neither the literal rule nor the connect-time guard
fires. So a save by a caller below admin also resolves the host (bounded, off the loop) and refuses the write with the same reason when ANY answer is
internal. ``PRIMER_EGRESS_ALLOW`` is honoured; a resolution failure or timeout does not refuse (the connect-time guard still applies); an admin's
save is never resolved.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from primer.common import netguard
from primer.model.provider import Toolset
from primer.toolset.toolset_checks import check_mcp_url_resolves_public
from primer.common.entity_checks import EntityCheckError
from tests._support.caller import caller as _ctx
from tests.toolset.test_mcp_egress_guard import _system_toolset


def _toolset(url: str) -> Toolset:
    return Toolset.model_validate({"id": "kb", "provider": "mcp", "config": {"transport": "http", "config": {"url": url}}})


class _Person:
    def __init__(self, admin: bool) -> None:
        self.is_admin = admin


@pytest.fixture(autouse=True)
def _no_allowlist():
    netguard.configure_egress_allow([])
    yield
    netguard.configure_egress_allow([])


def _resolver(*addresses: str, calls: list | None = None):
    async def resolve(host: str, port: int) -> list[str]:
        if calls is not None:
            calls.append((host, port))
        return list(addresses)

    return resolve


@pytest.mark.parametrize("address", ["10.0.0.5", "169.254.169.254", "127.0.0.1", "192.168.1.1", "fd00::5", "::ffff:10.1.2.3"])
async def test_a_name_resolving_internal_is_refused_for_a_non_admin(monkeypatch, address):
    monkeypatch.setattr(netguard, "_resolve", _resolver(address))

    with pytest.raises(EntityCheckError) as err:
        await check_mcp_url_resolves_public(_toolset("https://mcp.sneaky.example/mcp"), _Person(False))

    assert err.value.kind == "forbidden" and "internal address" in err.value.message


async def test_one_internal_answer_among_public_ones_refuses(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", _resolver("93.184.216.34", "10.0.0.5"))

    with pytest.raises(EntityCheckError):
        await check_mcp_url_resolves_public(_toolset("https://mcp.sneaky.example/mcp"), _Person(False))


async def test_a_public_resolution_is_allowed(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", _resolver("93.184.216.34", "2606:2800:220:1::1"))

    await check_mcp_url_resolves_public(_toolset("https://mcp.example.com/mcp"), _Person(False))


async def test_an_admin_save_is_never_resolved(monkeypatch):
    calls: list = []
    monkeypatch.setattr(netguard, "_resolve", _resolver("10.0.0.5", calls=calls))

    await check_mcp_url_resolves_public(_toolset("https://mcp.internal.example/mcp"), _Person(True))

    assert calls == []


async def test_the_operator_allowlist_is_honoured(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", _resolver("10.0.0.5"))
    netguard.configure_egress_allow(["10.0.0.0/8"])
    await check_mcp_url_resolves_public(_toolset("https://mcp.corp.example/mcp"), _Person(False))

    netguard.configure_egress_allow(["mcp.corp.example"])
    await check_mcp_url_resolves_public(_toolset("https://mcp.corp.example/mcp"), _Person(False))


@pytest.mark.parametrize("failure", [OSError("no such host"), TimeoutError()])
async def test_a_failed_resolution_does_not_refuse(monkeypatch, failure):
    async def broken(host: str, port: int) -> list[str]:
        raise failure

    monkeypatch.setattr(netguard, "_resolve", broken)

    await check_mcp_url_resolves_public(_toolset("https://mcp.unresolvable.example/mcp"), _Person(False))


async def test_a_slow_resolver_is_cut_off_and_does_not_refuse(monkeypatch):
    async def slow(host: str, port: int) -> list[str]:
        await asyncio.sleep(30)
        return ["10.0.0.5"]

    monkeypatch.setattr(netguard, "_resolve", slow)
    monkeypatch.setattr(netguard, "RESOLVE_TIMEOUT_SECONDS", 0.05)

    await check_mcp_url_resolves_public(_toolset("https://mcp.slow.example/mcp"), _Person(False))


async def test_stdio_python_and_literal_ip_urls_are_not_resolved(monkeypatch):
    calls: list = []
    monkeypatch.setattr(netguard, "_resolve", _resolver("10.0.0.5", calls=calls))
    stdio = Toolset.model_validate({"id": "s", "provider": "mcp", "config": {"transport": "stdio", "config": {"command": ["echo"]}}})

    await check_mcp_url_resolves_public(stdio, _Person(False))
    await check_mcp_url_resolves_public(_toolset("http://93.184.216.34/mcp"), _Person(False))

    assert calls == []


# ---- the system tools: a FULL grantee of an admin-owned toolset ---------------------------------------------------------------------------


async def test_the_update_tool_refuses_a_grantee_repointing_an_admin_toolset_at_an_internal_name(monkeypatch):
    toolset = _system_toolset()
    public = {"id": "kb", "provider": "mcp", "config": {"transport": "http", "config": {"url": "https://mcp.example.com/mcp"}}}
    assert not (await toolset.call(tool_name="create_toolset", arguments={"entity": public}, principal=None, ctx=_ctx("admin"))).is_error
    monkeypatch.setattr(netguard, "_resolve", _resolver("10.0.0.5"))
    repointed = {**public, "config": {"transport": "http", "config": {"url": "https://mcp.sneaky.example/mcp"}}}

    result = await toolset.call(tool_name="update_toolset", arguments={"id": "kb", "entity": repointed}, principal=None, ctx=_ctx("user"))

    assert result.is_error and json.loads(result.output)["type"] == "forbidden", result.output
    assert "internal address" in json.loads(result.output)["message"]
