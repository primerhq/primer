"""Tests for the shared google-genai SDK exception classifier."""

from __future__ import annotations

import httpx
import pytest
from google.genai import errors as gerrors

from matrix.common.google_errors import classify_google_exception
from matrix.model.except_ import (
    AuthenticationError,
    BadRequestError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
)


def _make_api_error(code: int, *, message: str = "test failure") -> gerrors.APIError:
    """Build a google-genai APIError with the SDK's actual constructor.

    The SDK's APIError.__init__ is `(code: int, response_json, response=None)`.
    The constructor parses `response_json` to derive `.message` and `.status`.
    We pass a minimal Google-shaped error dict to satisfy that parsing.
    """
    return gerrors.APIError(
        code,
        {"error": {"code": code, "message": message, "status": "TEST_STATUS"}},
    )


class TestClassifyGoogleException:
    def test_401_maps_to_authentication(self) -> None:
        sdk_exc = _make_api_error(401, message="auth fail")
        result = classify_google_exception(sdk_exc)
        assert isinstance(result, AuthenticationError)
        assert result.status_code == 401
        assert result.cause is sdk_exc

    def test_403_maps_to_authentication(self) -> None:
        sdk_exc = _make_api_error(403)
        result = classify_google_exception(sdk_exc)
        assert isinstance(result, AuthenticationError)
        assert result.status_code == 403

    def test_429_maps_to_rate_limit(self) -> None:
        sdk_exc = _make_api_error(429)
        result = classify_google_exception(sdk_exc)
        assert isinstance(result, RateLimitError)
        assert result.status_code == 429

    def test_400_maps_to_bad_request(self) -> None:
        sdk_exc = _make_api_error(400, message="bad argument")
        result = classify_google_exception(sdk_exc)
        assert isinstance(result, BadRequestError)
        assert result.status_code == 400

    def test_other_4xx_maps_to_bad_request(self) -> None:
        # 404 isn't an explicit branch, falls through the 4xx default.
        sdk_exc = _make_api_error(404)
        result = classify_google_exception(sdk_exc)
        assert isinstance(result, BadRequestError)
        assert result.status_code == 404

    def test_500_maps_to_server_error(self) -> None:
        sdk_exc = _make_api_error(500)
        result = classify_google_exception(sdk_exc)
        assert isinstance(result, ServerError)
        assert result.status_code == 500

    def test_503_maps_to_server_error(self) -> None:
        sdk_exc = _make_api_error(503)
        result = classify_google_exception(sdk_exc)
        assert isinstance(result, ServerError)
        assert result.status_code == 503

    def test_timeout_exception_maps_to_network_error(self) -> None:
        sdk_exc = httpx.TimeoutException("read timeout")
        result = classify_google_exception(sdk_exc)
        assert isinstance(result, NetworkError)
        assert result.cause is sdk_exc

    def test_connect_error_maps_to_network_error(self) -> None:
        sdk_exc = httpx.ConnectError("connection refused")
        result = classify_google_exception(sdk_exc)
        assert isinstance(result, NetworkError)

    def test_arbitrary_exception_maps_to_provider_error(self) -> None:
        sdk_exc = RuntimeError("totally unexpected")
        result = classify_google_exception(sdk_exc)
        assert isinstance(result, ProviderError)
        assert "totally unexpected" in str(result)
        assert result.cause is sdk_exc
