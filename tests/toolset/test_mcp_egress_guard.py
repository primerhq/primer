"""A user cannot point an MCP toolset at an internal address (review of workspaces, W25).

An http / sse MCP toolset is user-tier, and its server POSTs JSON-RPC to the URL on create (the probe) and on every listing: ``169.254.169.254``,
``10.0.0.5:5432`` or the Kubernetes API were reachable, and the probe's error text scanned the internal network. Now:

* SAVE: a URL whose host is an internal address literal (or ``localhost``) is reserved to an admin, through the one rule the REST route, the system
  tools and a harness install share (``toolset_admin_reason``). An admin may still configure an internal server.
* CONNECT: a toolset an admin did not author connects through the egress guard, which resolves the name and refuses any internal answer at connect
  time (so a name re-bound after the save is refused too) and connects to the vetted address. ``PRIMER_EGRESS_ALLOW`` still opts internal servers in.
"""

from __future__ import annotations

import pytest

from primer.common import netguard
from primer.model.except_ import ConfigError
from primer.model.provider import HttpConfig, McpConfig, Toolset, TransportType
from primer.toolset.mcp import McpEgressRefused, McpToolsetProvider
from primer.toolset.toolset_checks import toolset_admin_reason


def _toolset(url: str, *, transport: str = "http") -> Toolset:
    return Toolset.model_validate({"id": "kb", "provider": "mcp", "config": {"transport": transport, "config": {"url": url}}})


INTERNAL = [
    "http://127.0.0.1:1/mcp", "http://localhost:8080/mcp", "http://LOCALHOST./mcp", "http://api.localhost/mcp",
    "http://169.254.169.254/latest/meta-data", "http://10.0.0.5:5432", "http://192.168.1.10/mcp", "http://172.16.0.9/mcp",
    "http://[::1]:9/mcp", "http://[fe80::1]/mcp", "http://[fd00::5]/mcp", "http://[::ffff:10.1.2.3]/mcp", "http://0.0.0.0/mcp",
    "http://100.64.0.1/mcp",
]
PUBLIC = ["https://mcp.example.com/mcp", "http://93.184.216.34/mcp", "https://[2606:2800:220:1::1]/mcp", "https://localhost.example.com/mcp"]


@pytest.fixture(autouse=True)
def _no_operator_allowlist():
    netguard.configure_egress_allow([])
    yield
    netguard.configure_egress_allow([])


@pytest.mark.parametrize("url", INTERNAL)
@pytest.mark.parametrize("transport", ["http", "sse"])
def test_an_internal_url_is_reserved_to_an_admin(url, transport):
    reason = toolset_admin_reason(_toolset(url, transport=transport))

    assert reason is not None and "internal" in reason and "admin" in reason


@pytest.mark.parametrize("url", PUBLIC)
def test_a_public_url_stays_user_tier(url):
    assert toolset_admin_reason(_toolset(url)) is None


def test_a_non_admin_cannot_repoint_a_public_toolset_at_an_internal_address():
    assert toolset_admin_reason(_toolset("http://10.0.0.5/mcp"), _toolset("https://mcp.example.com/mcp")) is not None


def test_the_operator_allowlist_opts_an_internal_server_in():
    netguard.configure_egress_allow(["10.0.0.0/8", "mcp.internal"])

    assert toolset_admin_reason(_toolset("http://10.0.0.5/mcp")) is None
    assert toolset_admin_reason(_toolset("http://mcp.internal/mcp")) is None
    assert toolset_admin_reason(_toolset("http://192.168.0.1/mcp")) is not None


def _provider(url: str, *, guard) -> McpToolsetProvider:
    return McpToolsetProvider("kb", McpConfig(transport=TransportType.HTTP, config=HttpConfig(url=url)), egress_guard=guard)


async def _list(provider: McpToolsetProvider) -> None:
    async for _tool in provider.list_tools():
        pass


async def _guarded() -> bool:
    return True


async def _unguarded() -> bool:
    return False


@pytest.mark.parametrize("guard", [True, _guarded], ids=["flag", "callable"])
async def test_a_guarded_toolset_at_a_loopback_literal_is_refused_before_any_connection(guard):
    provider = _provider("http://127.0.0.1:1/mcp", guard=guard)

    with pytest.raises(McpEgressRefused) as err:
        await _list(provider)

    assert "private address" in str(err.value) and isinstance(err.value, ConfigError)


async def test_a_name_that_resolves_internal_at_connect_time_is_refused(monkeypatch):
    """DNS rebinding: the name passed the save (it looked public); at connect time it answers with a private address."""
    async def rebound(host: str, port: int) -> list[str]:
        return ["10.0.0.5"]

    monkeypatch.setattr(netguard, "_resolve", rebound)

    with pytest.raises(McpEgressRefused):
        await _list(_provider("http://mcp.rebound.example/mcp", guard=True))


async def test_a_trusted_toolset_is_not_guarded(monkeypatch):
    """An admin-authored toolset (the guard callable answers False) connects as before: the guard never runs (the closed port refuses it)."""
    called: list[str] = []

    async def spy(host: str, port: int) -> list[str]:
        called.append(host)
        return ["127.0.0.1"]

    monkeypatch.setattr(netguard, "_resolve", spy)

    with pytest.raises(Exception) as err:
        await _list(_provider("http://127.0.0.1:1/mcp", guard=_unguarded))

    assert not isinstance(err.value, McpEgressRefused) and called == []


async def test_the_allowlist_lets_a_guarded_toolset_reach_an_internal_server(monkeypatch):
    netguard.configure_egress_allow(["127.0.0.1"])

    with pytest.raises(Exception) as err:  # allowed by the guard; nothing listens on port 1
        await _list(_provider("http://127.0.0.1:1/mcp", guard=True))

    assert not isinstance(err.value, McpEgressRefused)


async def test_the_sse_transport_is_guarded_too():
    provider = McpToolsetProvider("kb", McpConfig(transport=TransportType.SSE, config=HttpConfig(url="http://169.254.169.254/sse")), egress_guard=True)

    with pytest.raises(McpEgressRefused):
        await _list(provider)


# ---- the doors: the system tools, the REST route, the registry -------------------------------------------------------------------------------

import json  # noqa: E402

from primer.api.registries import ProviderRegistry  # noqa: E402
from primer.toolset.system import build_system_toolset  # noqa: E402
from tests._support.caller import caller as _ctx  # noqa: E402
from tests.conftest import _FakeStorageProvider  # noqa: E402


def _system_toolset():
    sp = _FakeStorageProvider()
    reg = ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda p: object(),
    )
    return build_system_toolset(storage_provider=sp, provider_registry=reg)


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest", "http://10.0.0.5:5432/mcp", "http://127.0.0.1:1/mcp", "http://localhost/mcp"])
async def test_the_create_tool_refuses_an_internal_url_below_admin_and_allows_an_admin(url):
    toolset = _system_toolset()
    body = {"id": "kb", "provider": "mcp", "config": {"transport": "http", "config": {"url": url}}}

    refused = await toolset.call(tool_name="create_toolset", arguments={"entity": body}, principal=None, ctx=_ctx("user"))
    assert refused.is_error and json.loads(refused.output)["type"] == "forbidden", refused.output
    assert "internal address" in json.loads(refused.output)["message"]

    allowed = await toolset.call(tool_name="create_toolset", arguments={"entity": body}, principal=None, ctx=_ctx("admin"))
    assert not allowed.is_error, allowed.output


async def test_the_create_tool_still_lets_a_user_save_a_public_server():
    body = {"id": "kb", "provider": "mcp", "config": {"transport": "http", "config": {"url": "https://mcp.example.com/mcp"}}}

    result = await _system_toolset().call(tool_name="create_toolset", arguments={"entity": body}, principal=None, ctx=_ctx("user"))

    assert not result.is_error, result.output


async def _registry_guard(owners: list[str], users: dict[str, str], *, disabled: tuple[str, ...] = ()) -> bool:
    """What the registry's per-dispatch decision says (True: guarded) for a stored toolset owned by ``owners``."""
    from primer.api.registries.provider_registry import _build_default_toolset_factory
    from primer.model.access import Access
    from primer.model.user import User

    sp = _FakeStorageProvider()
    for uid, role in users.items():
        await sp.get_storage(User).create(User(id=uid, username=uid, role=role, disabled=uid in disabled, created_at=_now()))
    row = _toolset("http://10.0.0.5/mcp").model_copy(update={"access": Access(owners=owners)})
    await sp.get_storage(Toolset).create(row)
    provider = _build_default_toolset_factory(storage_provider=sp)(row)
    return await provider._egress_guard()  # type: ignore[operator]


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def test_the_registry_guards_a_toolset_no_admin_authored():
    assert await _registry_guard(["u-user"], {"u-user": "user"}) is True


async def test_the_registry_trusts_a_toolset_an_enabled_admin_owns_and_one_with_no_owner():
    assert await _registry_guard(["u-admin"], {"u-admin": "admin"}) is False
    assert await _registry_guard(["u-user", "u-admin"], {"u-user": "user", "u-admin": "admin"}) is False
    assert await _registry_guard([], {}) is False, "a platform-seeded or harness-installed row has no owner: admin-only to write"


async def test_the_registry_guards_a_toolset_whose_admin_owner_is_disabled_or_demoted():
    assert await _registry_guard(["u-admin"], {"u-admin": "admin"}, disabled=("u-admin",)) is True
    assert await _registry_guard(["u-admin"], {"u-admin": "user"}) is True


async def test_a_registry_built_without_storage_guards_nothing():
    from primer.api.registries.provider_registry import _build_default_toolset_factory

    provider = _build_default_toolset_factory()(_toolset("http://10.0.0.5/mcp"))

    assert provider._egress_guard is False
