"""Tests for primer.common.mcp_errors.classify_mcp_exception."""

from __future__ import annotations

import httpx
import httpx2
import pytest
from mcp.shared.exceptions import MCPError

from primer.common.mcp_errors import classify_mcp_exception
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
)


# mcp>=2.0's transports raise httpx2 exceptions (no ancestry with plain
# httpx); primer's own layers still raise httpx. The classifier must map
# both families identically, so every mapping test runs against each.
@pytest.fixture(params=[httpx, httpx2], ids=["httpx", "httpx2"])
def lib(request):
    return request.param


def _http_status_error(status: int, lib=httpx):
    request = lib.Request("POST", "https://example.test/mcp")
    response = lib.Response(status, request=request)
    return lib.HTTPStatusError(
        f"status {status}", request=request, response=response
    )


class TestHttpStatusErrorMapping:
    def test_401_maps_to_authentication_error(self, lib) -> None:
        result = classify_mcp_exception(_http_status_error(401, lib))
        assert isinstance(result, AuthenticationError)
        assert result.status_code == 401

    def test_403_maps_to_authentication_error(self, lib) -> None:
        result = classify_mcp_exception(_http_status_error(403, lib))
        assert isinstance(result, AuthenticationError)
        assert result.status_code == 403

    def test_429_maps_to_rate_limit(self, lib) -> None:
        result = classify_mcp_exception(_http_status_error(429, lib))
        assert isinstance(result, RateLimitError)
        assert result.status_code == 429

    def test_400_maps_to_bad_request(self, lib) -> None:
        result = classify_mcp_exception(_http_status_error(400, lib))
        assert isinstance(result, BadRequestError)
        assert result.status_code == 400

    def test_500_maps_to_server_error(self, lib) -> None:
        result = classify_mcp_exception(_http_status_error(503, lib))
        assert isinstance(result, ServerError)
        assert result.status_code == 503

    def test_other_4xx_maps_to_provider_error(self, lib) -> None:
        result = classify_mcp_exception(_http_status_error(418, lib))
        assert isinstance(result, ProviderError)
        assert not isinstance(
            result, (AuthenticationError, BadRequestError, RateLimitError, ServerError)
        )
        assert result.status_code == 418


class TestMcpErrorMapping:
    def test_mcp_error_with_message_becomes_provider_error(self) -> None:
        exc = MCPError(code=-32000, message="server boom")
        result = classify_mcp_exception(exc)
        assert isinstance(result, ProviderError)
        assert "server boom" in str(result)


class TestNetworkErrors:
    def test_timeout_maps_to_network_error(self, lib) -> None:
        exc = lib.ReadTimeout("timed out")
        result = classify_mcp_exception(exc)
        assert isinstance(result, NetworkError)

    def test_network_error_maps_to_network_error(self, lib) -> None:
        exc = lib.ConnectError("refused")
        result = classify_mcp_exception(exc)
        assert isinstance(result, NetworkError)

    def test_httpx2_families_share_no_httpx_ancestry(self) -> None:
        """The premise this whole parametrization exists for: if httpx2
        ever gains httpx ancestry these become redundant, until then a
        plain-httpx-only classifier silently misclassifies transport
        failures from the mcp SDK."""
        assert not issubclass(httpx2.ConnectError, httpx.ConnectError)
        assert not issubclass(httpx2.TimeoutException, httpx.TimeoutException)
        assert not issubclass(httpx2.HTTPStatusError, httpx.HTTPStatusError)


class TestUnknownExceptions:
    def test_random_exception_becomes_provider_error(self) -> None:
        exc = ValueError("nope")
        result = classify_mcp_exception(exc)
        assert isinstance(result, ProviderError)
        assert result.cause is exc
