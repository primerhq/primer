"""The private-destination guard on the OAuth endpoints an MCP server names (audit G4-08; :mod:`primer.toolset.oauth.egress`).

No real network for the routing rules: the resolver of :mod:`primer.common.ssrf` is replaced and the client runs over a recording httpcore mock
backend (the one the netguard tests use), so a test sees which address a connection was opened to. The last tests run the service against the
hermetic mock server for real: it listens on 127.0.0.1, and the toolset names it ``localhost``, so every endpoint it advertises is on another,
loopback host.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from primer.common import ssrf
from primer.common.secret_box import SecretBox
from primer.model.except_ import BadRequestError
from primer.toolset.oauth import egress
from primer.toolset.oauth.egress import calls_for, oauth_http_client
from primer.toolset.oauth.protocol import discover
from primer.toolset.oauth.service import ToolsetOAuthService
from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp  # noqa: F401 - the fixture
from tests.common.test_netguard import _OK, _RecordingBackend
from tests.toolset.oauth._service_helpers import make_toolset, target_of

PUBLIC = "93.184.216.34"


@pytest.fixture(autouse=True)
def _guard_on():
    ssrf.configure_allow_private_destinations(False)
    yield
    ssrf.configure_allow_private_destinations(False)


@pytest.fixture
def dns(monkeypatch) -> list[str]:
    """Names resolve through this table (an IP literal never reaches it); returns the lookup log."""
    table = {"as.example": [PUBLIC], "as.internal": ["10.0.0.5"], "mixed.example": [PUBLIC, "169.254.169.254"], "mcp.example": [PUBLIC]}
    looked_up: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        looked_up.append(host)
        return list(table.get(host, []))

    monkeypatch.setattr(ssrf, "_resolve", resolve)
    return looked_up


def _client(responses: int = 4) -> tuple[httpx.AsyncClient, _RecordingBackend]:
    backend = _RecordingBackend([_OK] * responses)
    return oauth_http_client(timeout=5.0, network_backend=backend), backend


async def test_the_mcp_servers_own_host_is_reached_directly(dns):
    client, backend = _client()
    async with client:
        with calls_for("http://mcp.internal:8080/mcp"):
            assert (await client.get("http://mcp.internal:8080/.well-known/oauth-protected-resource/mcp")).status_code == 200
    assert backend.log["connect"] == [("mcp.internal", 8080)] and dns == [], "the MCP host is not vetted: the toolset's author chose it"


@pytest.mark.parametrize("url", [
    "http://as.internal/token",                                 # a name that resolves to RFC 1918
    "http://mixed.example/token",                               # one public record and one metadata record
    "http://169.254.169.254/latest/meta-data/",                 # the cloud metadata address
    "http://127.0.0.1:6379/prm",                                # loopback
    "http://[::1]:8080/register",                               # IPv6 loopback
    "http://10.0.0.5/revoke",
])
async def test_another_host_on_a_private_address_is_refused_before_any_connection(dns, url):
    client, backend = _client()
    async with client:
        with calls_for("https://mcp.example/mcp"), pytest.raises(ssrf.BlockedDestinationError, match="is not a public address"):
            await client.post(url, data={"a": "b"})
    assert backend.log["connect"] == []


async def test_another_host_on_a_public_address_connects_to_the_vetted_address(dns):
    client, backend = _client()
    async with client:
        with calls_for("https://mcp.example/mcp"):
            assert (await client.get("https://as.example/.well-known/oauth-authorization-server")).status_code == 200
    assert backend.log["connect"] == [(PUBLIC, 443)], "the connection goes to the address that was vetted, so a re-bound name cannot slip through"
    assert backend.log["sni"] == ["as.example"], "the certificate is still checked against the name"


async def test_outside_any_toolset_nothing_is_direct(dns):
    client, backend = _client()
    async with client:
        with pytest.raises(ssrf.BlockedDestinationError):
            await client.get("http://127.0.0.1:9/mcp")
    assert backend.log["connect"] == [] and egress.direct_hosts() == frozenset()


async def test_one_toolsets_mcp_host_is_not_direct_for_another(dns):
    """A connection opened directly to toolset A's MCP host is never reused for toolset B's request to that host: B's goes through the guard."""
    client, backend = _client()
    async with client:
        with calls_for("http://127.0.0.1:9/mcp"):
            assert (await client.get("http://127.0.0.1:9/mcp")).status_code == 200
        with calls_for("https://mcp.example/mcp"), pytest.raises(ssrf.BlockedDestinationError):
            await client.get("http://127.0.0.1:9/token")
    assert backend.log["connect"] == [("127.0.0.1", 9)]


async def test_a_token_endpoint_the_toolset_configures_is_reached_directly(dns):
    client, backend = _client()
    async with client:
        with calls_for("https://mcp.example/mcp", "http://as.internal/token"):
            assert (await client.post("http://as.internal/token", data={"grant_type": "client_credentials"})).status_code == 200
    assert backend.log["connect"] == [("as.internal", 80)]


async def test_the_operators_opt_in_lets_private_addresses_through(dns):
    """The existing opt-in of primer.common.ssrf (workspace_allow_private_url_sources) covers these endpoints too; no other setting exists."""
    ssrf.configure_allow_private_destinations(True)
    client, backend = _client()
    async with client:
        with calls_for("https://mcp.example/mcp"):
            assert (await client.post("http://as.internal/token", data={"a": "b"})).status_code == 200
    assert backend.log["connect"] == [("10.0.0.5", 80)]


async def test_discovery_does_not_follow_a_challenge_to_a_private_address(dns):
    """The scratch case of the audit: the 401 names a protected-resource document on loopback, whose authorization server is the metadata address."""
    client, backend = _client()
    async with client:
        with calls_for("https://mcp.example/mcp"), pytest.raises(BadRequestError, match=r"http://127\.0\.0\.1:6379/prm is not a public address"):
            await discover("https://mcp.example/mcp", www_authenticate='Bearer resource_metadata="http://127.0.0.1:6379/prm"', http=client)
    assert backend.log["connect"] == []


async def test_respx_still_intercepts_the_routed_client(dns):
    """The service tests mock the OAuth endpoints with respx; the guard lives below the pool respx patches, so those tests never resolve a name."""
    async with oauth_http_client(timeout=5.0) as client:
        with respx.mock() as router:
            router.get("https://as.internal/x").mock(return_value=httpx.Response(204))
            assert (await client.get("https://as.internal/x")).status_code == 204
    assert dns == []


# ---- through the service, against the mock server ----------------------------------------------------------------------------------------------------------------------


async def test_a_sign_in_never_calls_server_named_endpoints_on_another_private_host(fake_storage_provider, mock_oauth_mcp):
    """Red on main: the toolset names the mock as ``localhost``; everything the mock advertises is on 127.0.0.1, another host, and loopback. The
    client-credentials connect discovers its token endpoint from that metadata, so it must stop at the first server-named URL."""
    port = mock_oauth_mcp.base_url.rsplit(":", 1)[1]
    toolset = make_toolset(f"http://localhost:{port}/mcp", oauth={"grant": "client_credentials", "static_client": {"client_id": "cc-client",
                                                                                                                   "client_secret": "cc-secret"}})
    service = ToolsetOAuthService(storage_provider=fake_storage_provider, secret_box=SecretBox.from_key_material("egress-test-key-0123456789"))
    try:
        with pytest.raises(BadRequestError, match=r"http://127\.0\.0\.1:\d+/\.well-known/oauth-protected-resource/mcp is not a public address"):
            await service.connect_client_credentials(target_of(toolset), connected_by="admin")
    finally:
        await service.aclose()
    assert mock_oauth_mcp.state.token_requests == [] and await service.store.get_tokens("kb") is None
