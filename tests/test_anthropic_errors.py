"""Tests for the shared anthropic SDK exception classifier."""

from __future__ import annotations

import anthropic
import pytest

from matrix.common.anthropic_errors import classify_anthropic_exception
from matrix.model.except_ import (
    AuthenticationError,
    BadRequestError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
)


def _make_anthropic_error(cls: type, *, status_code: int = 400, code: str | None = None):
    exc = cls.__new__(cls)
    exc.status_code = status_code
    exc.code = code
    exc.message = f"test {cls.__name__}"
    Exception.__init__(exc, exc.message)
    return exc


class TestClassifyAnthropicException:
    def test_authentication_error(self) -> None:
        sdk_exc = _make_anthropic_error(anthropic.AuthenticationError, status_code=401)
        result = classify_anthropic_exception(sdk_exc)
        assert isinstance(result, AuthenticationError)
        assert result.cause is sdk_exc

    def test_permission_denied_maps_to_authentication(self) -> None:
        sdk_exc = _make_anthropic_error(anthropic.PermissionDeniedError, status_code=403)
        result = classify_anthropic_exception(sdk_exc)
        assert isinstance(result, AuthenticationError)

    def test_rate_limit(self) -> None:
        sdk_exc = _make_anthropic_error(anthropic.RateLimitError, status_code=429)
        result = classify_anthropic_exception(sdk_exc)
        assert isinstance(result, RateLimitError)

    def test_bad_request(self) -> None:
        sdk_exc = _make_anthropic_error(anthropic.BadRequestError, status_code=400)
        result = classify_anthropic_exception(sdk_exc)
        assert isinstance(result, BadRequestError)

    def test_not_found_maps_to_bad_request(self) -> None:
        sdk_exc = _make_anthropic_error(anthropic.NotFoundError, status_code=404)
        result = classify_anthropic_exception(sdk_exc)
        assert isinstance(result, BadRequestError)

    def test_unprocessable_maps_to_bad_request(self) -> None:
        sdk_exc = _make_anthropic_error(anthropic.UnprocessableEntityError, status_code=422)
        result = classify_anthropic_exception(sdk_exc)
        assert isinstance(result, BadRequestError)

    def test_internal_server_error(self) -> None:
        sdk_exc = _make_anthropic_error(anthropic.InternalServerError, status_code=500)
        result = classify_anthropic_exception(sdk_exc)
        assert isinstance(result, ServerError)

    def test_5xx_apistatus_to_server(self) -> None:
        sdk_exc = _make_anthropic_error(anthropic.APIStatusError, status_code=503)
        result = classify_anthropic_exception(sdk_exc)
        assert isinstance(result, ServerError)

    def test_other_4xx_apistatus_to_provider(self) -> None:
        sdk_exc = _make_anthropic_error(anthropic.APIStatusError, status_code=499)
        result = classify_anthropic_exception(sdk_exc)
        assert isinstance(result, ProviderError)
        assert not isinstance(result, (BadRequestError, ServerError))

    def test_unknown_exception(self) -> None:
        sdk_exc = RuntimeError("unexpected")
        result = classify_anthropic_exception(sdk_exc)
        assert isinstance(result, ProviderError)
        assert "unexpected" in str(result)
