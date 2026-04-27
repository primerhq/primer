"""Tests for matrix.common.mcp_errors.classify_mcp_exception."""

from __future__ import annotations

import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from matrix.common.mcp_errors import classify_mcp_exception
from matrix.model.except_ import (
    AuthenticationError,
    BadRequestError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/mcp")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"status {status}", request=request, response=response
    )


class TestHttpStatusErrorMapping:
    def test_401_maps_to_authentication_error(self) -> None:
        result = classify_mcp_exception(_http_status_error(401))
        assert isinstance(result, AuthenticationError)
        assert result.status_code == 401

    def test_403_maps_to_authentication_error(self) -> None:
        result = classify_mcp_exception(_http_status_error(403))
        assert isinstance(result, AuthenticationError)
        assert result.status_code == 403

    def test_429_maps_to_rate_limit(self) -> None:
        result = classify_mcp_exception(_http_status_error(429))
        assert isinstance(result, RateLimitError)
        assert result.status_code == 429

    def test_400_maps_to_bad_request(self) -> None:
        result = classify_mcp_exception(_http_status_error(400))
        assert isinstance(result, BadRequestError)
        assert result.status_code == 400

    def test_500_maps_to_server_error(self) -> None:
        result = classify_mcp_exception(_http_status_error(503))
        assert isinstance(result, ServerError)
        assert result.status_code == 503

    def test_other_4xx_maps_to_provider_error(self) -> None:
        result = classify_mcp_exception(_http_status_error(418))
        assert isinstance(result, ProviderError)
        assert not isinstance(
            result, (AuthenticationError, BadRequestError, RateLimitError, ServerError)
        )
        assert result.status_code == 418


class TestMcpErrorMapping:
    def test_mcp_error_with_message_becomes_provider_error(self) -> None:
        exc = McpError(ErrorData(code=-32000, message="server boom"))
        result = classify_mcp_exception(exc)
        assert isinstance(result, ProviderError)
        assert "server boom" in str(result)


class TestNetworkErrors:
    def test_timeout_maps_to_network_error(self) -> None:
        exc = httpx.ReadTimeout("timed out")
        result = classify_mcp_exception(exc)
        assert isinstance(result, NetworkError)

    def test_network_error_maps_to_network_error(self) -> None:
        exc = httpx.ConnectError("refused")
        result = classify_mcp_exception(exc)
        assert isinstance(result, NetworkError)


class TestUnknownExceptions:
    def test_random_exception_becomes_provider_error(self) -> None:
        exc = ValueError("nope")
        result = classify_mcp_exception(exc)
        assert isinstance(result, ProviderError)
        assert result.cause is exc
