"""Unit tests for the matrix exception hierarchy."""

from __future__ import annotations

import pytest

from matrix.model.except_ import (
    AuthenticationError,
    AuthRequiredError,
    BadRequestError,
    ConfigError,
    LeaseLostError,
    MatrixError,
    ModelNotFoundError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
    TransientError,
    TurnConflictError,
    UnsupportedContentError,
)


# ============================================================================
# MatrixError base — fields, str() formatting, cause chaining
# ============================================================================


class TestMatrixError:
    def test_message_only(self):
        exc = MatrixError("oops")
        assert exc.message == "oops"
        assert exc.code is None
        assert exc.status_code is None
        assert exc.cause is None
        assert str(exc) == "oops"

    def test_with_code(self):
        exc = MatrixError("oops", code="ERR_X")
        assert exc.code == "ERR_X"
        assert str(exc) == "[ERR_X] oops"

    def test_with_status_code(self):
        exc = MatrixError("oops", status_code=500)
        assert exc.status_code == 500
        assert str(exc) == "[500] oops"

    def test_with_code_and_status(self):
        exc = MatrixError("oops", code="ERR_X", status_code=500)
        assert str(exc) == "[500 ERR_X] oops"

    def test_with_cause_sets_dunder_cause(self):
        original = ValueError("inner")
        exc = MatrixError("outer", cause=original)
        assert exc.cause is original
        assert exc.__cause__ is original

    def test_can_be_raised(self):
        with pytest.raises(MatrixError, match="oops"):
            raise MatrixError("oops")

    def test_inherits_from_exception(self):
        assert issubclass(MatrixError, Exception)

    def test_message_is_required(self):
        with pytest.raises(TypeError):
            MatrixError()  # type: ignore[call-arg]


# ============================================================================
# Inheritance chain — every leaf class is a MatrixError
# ============================================================================


class TestExceptionHierarchy:
    def test_config_error_chain(self):
        assert issubclass(ConfigError, MatrixError)

    def test_model_not_found_error_chain(self):
        assert issubclass(ModelNotFoundError, ConfigError)
        assert issubclass(ModelNotFoundError, MatrixError)

    def test_unsupported_content_error_chain(self):
        assert issubclass(UnsupportedContentError, MatrixError)
        assert not issubclass(UnsupportedContentError, ConfigError)
        assert not issubclass(UnsupportedContentError, ProviderError)

    def test_provider_error_chain(self):
        assert issubclass(ProviderError, MatrixError)

    def test_authentication_error_chain(self):
        assert issubclass(AuthenticationError, ProviderError)
        assert issubclass(AuthenticationError, MatrixError)

    def test_rate_limit_error_chain(self):
        assert issubclass(RateLimitError, ProviderError)
        assert issubclass(RateLimitError, MatrixError)

    def test_bad_request_error_chain(self):
        assert issubclass(BadRequestError, ProviderError)
        assert issubclass(BadRequestError, MatrixError)

    def test_server_error_chain(self):
        assert issubclass(ServerError, ProviderError)
        assert issubclass(ServerError, MatrixError)

    def test_network_error_chain(self):
        assert issubclass(NetworkError, MatrixError)
        assert not issubclass(NetworkError, ProviderError)


# ============================================================================
# Concrete subclass behaviour — fields, isinstance, raising
# ============================================================================


class TestConcreteSubclasses:
    def test_authentication_error_full(self):
        exc = AuthenticationError(
            "invalid api key",
            code="invalid_api_key",
            status_code=401,
        )
        assert isinstance(exc, ProviderError)
        assert isinstance(exc, MatrixError)
        assert exc.code == "invalid_api_key"
        assert exc.status_code == 401
        assert str(exc) == "[401 invalid_api_key] invalid api key"

    def test_rate_limit_error_with_cause(self):
        cause = TimeoutError("network busy")
        exc = RateLimitError("rate limited", status_code=429, cause=cause)
        assert exc.cause is cause
        assert exc.__cause__ is cause
        assert exc.status_code == 429

    def test_bad_request_error_construction(self):
        exc = BadRequestError("invalid params", status_code=400)
        assert isinstance(exc, ProviderError)
        assert str(exc) == "[400] invalid params"

    def test_server_error_construction(self):
        exc = ServerError("upstream crashed", status_code=503)
        assert isinstance(exc, ProviderError)
        assert str(exc) == "[503] upstream crashed"

    def test_model_not_found_error_construction(self):
        exc = ModelNotFoundError("model 'gpt-99' not in declared list")
        assert isinstance(exc, ConfigError)
        assert isinstance(exc, MatrixError)

    def test_unsupported_content_error_construction(self):
        exc = UnsupportedContentError("AudioPart not supported by Anthropic")
        assert isinstance(exc, MatrixError)

    def test_network_error_construction(self):
        exc = NetworkError("connection refused", cause=ConnectionRefusedError())
        assert isinstance(exc, MatrixError)
        assert isinstance(exc.cause, ConnectionRefusedError)

    def test_raise_from_clause_works(self):
        try:
            try:
                raise ValueError("inner")
            except ValueError as e:
                raise BadRequestError("outer", status_code=400, cause=e)
        except BadRequestError as exc:
            assert exc.__cause__.__class__ is ValueError
            assert str(exc.__cause__) == "inner"


# ============================================================================
# AuthRequiredError — OAuth consent-required signal carrying auth URL + state
# ============================================================================


class TestAuthRequiredError:
    def test_carries_auth_url_and_state(self) -> None:
        e = AuthRequiredError(
            "consent required",
            auth_url="https://idp.example/auth?code=...",
            state="state-uuid-1",
        )
        assert e.auth_url == "https://idp.example/auth?code=..."
        assert e.state == "state-uuid-1"
        assert "consent required" in str(e)

    def test_inherits_matrix_error(self) -> None:
        e = AuthRequiredError(
            "x",
            auth_url="https://idp.example/auth",
            state="s",
        )
        assert isinstance(e, MatrixError)

    def test_optional_fields_default_to_none(self) -> None:
        e = AuthRequiredError(
            "x",
            auth_url="https://idp.example/auth",
            state="s",
        )
        assert e.code is None
        assert e.status_code is None
        assert e.cause is None

    def test_cause_is_chained(self) -> None:
        underlying = ValueError("nope")
        e = AuthRequiredError(
            "x",
            auth_url="https://idp.example/auth",
            state="s",
            cause=underlying,
        )
        assert e.cause is underlying
        assert e.__cause__ is underlying


# ============================================================================
# Background-execution scheduler error types
# ============================================================================


def test_transient_error_is_matrix_error():
    assert issubclass(TransientError, MatrixError)


def test_lease_lost_error_is_matrix_error():
    assert issubclass(LeaseLostError, MatrixError)


def test_turn_conflict_error_is_matrix_error():
    assert issubclass(TurnConflictError, MatrixError)


def test_transient_error_carries_message():
    exc = TransientError("network blip")
    assert "network blip" in str(exc)
