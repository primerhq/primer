"""Tests for the shared openai SDK exception classifier."""

from __future__ import annotations

from unittest.mock import MagicMock

import openai
import pytest

from primer.common.openai_errors import classify_openai_exception
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
)


def _make_openai_error(cls: type, *, status_code: int = 400, code: str | None = None):
    """Build an openai SDK exception with minimal init plumbing.

    The SDK's exception constructors require a Response and body in
    real use; for tests we bypass __init__ and set the relevant
    attributes directly.
    """
    exc = cls.__new__(cls)
    exc.status_code = status_code
    exc.code = code
    exc.message = f"test {cls.__name__}"
    Exception.__init__(exc, exc.message)
    return exc


class TestClassifyOpenaiException:
    def test_authentication_error_maps_to_primer_authentication(self) -> None:
        sdk_exc = _make_openai_error(openai.AuthenticationError, status_code=401)
        result = classify_openai_exception(sdk_exc)
        assert isinstance(result, AuthenticationError)
        assert result.status_code == 401
        assert result.cause is sdk_exc

    def test_rate_limit_error_maps_to_primer_rate_limit(self) -> None:
        sdk_exc = _make_openai_error(openai.RateLimitError, status_code=429)
        result = classify_openai_exception(sdk_exc)
        assert isinstance(result, RateLimitError)
        assert result.status_code == 429

    def test_bad_request_error_maps_to_primer_bad_request(self) -> None:
        sdk_exc = _make_openai_error(
            openai.BadRequestError, status_code=400, code="invalid_value"
        )
        result = classify_openai_exception(sdk_exc)
        assert isinstance(result, BadRequestError)
        assert result.status_code == 400
        assert result.code == "invalid_value"

    def test_internal_server_error_maps_to_primer_server(self) -> None:
        sdk_exc = _make_openai_error(openai.InternalServerError, status_code=500)
        result = classify_openai_exception(sdk_exc)
        assert isinstance(result, ServerError)
        assert result.status_code == 500

    def test_5xx_api_status_error_maps_to_primer_server(self) -> None:
        sdk_exc = _make_openai_error(openai.APIStatusError, status_code=503)
        result = classify_openai_exception(sdk_exc)
        assert isinstance(result, ServerError)
        assert result.status_code == 503

    def test_other_4xx_api_status_error_maps_to_provider_error(self) -> None:
        sdk_exc = _make_openai_error(openai.APIStatusError, status_code=404)
        result = classify_openai_exception(sdk_exc)
        assert isinstance(result, ProviderError)
        assert not isinstance(
            result, (AuthenticationError, RateLimitError, BadRequestError, ServerError)
        )
        assert result.status_code == 404

    def test_connection_error_maps_to_network_error(self) -> None:
        sdk_exc = openai.APIConnectionError(request=MagicMock())  # type: ignore[arg-type]
        result = classify_openai_exception(sdk_exc)
        assert isinstance(result, NetworkError)
        assert result.cause is sdk_exc

    def test_timeout_error_maps_to_network_error(self) -> None:
        sdk_exc = openai.APITimeoutError(request=MagicMock())  # type: ignore[arg-type]
        result = classify_openai_exception(sdk_exc)
        assert isinstance(result, NetworkError)

    def test_unknown_exception_maps_to_provider_error(self) -> None:
        sdk_exc = RuntimeError("totally unexpected")
        result = classify_openai_exception(sdk_exc)
        assert isinstance(result, ProviderError)
        assert "totally unexpected" in str(result)
        assert result.cause is sdk_exc
