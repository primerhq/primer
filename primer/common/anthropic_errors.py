"""Shared anthropic SDK exception classifier.

Used by every matrix adapter that wraps anthropic.AsyncAnthropic.
Maps the anthropic exception hierarchy (which mirrors openai's class
names) to the matrix exception hierarchy.
"""

from __future__ import annotations

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError as AnthropicAuthenticationError,
    BadRequestError as AnthropicBadRequestError,
    ConflictError as AnthropicConflictError,
    InternalServerError as AnthropicInternalServerError,
    NotFoundError as AnthropicNotFoundError,
    PermissionDeniedError as AnthropicPermissionDeniedError,
    RateLimitError as AnthropicRateLimitError,
    UnprocessableEntityError as AnthropicUnprocessableEntityError,
)

from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    MatrixError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
)


def classify_anthropic_exception(exc: Exception) -> MatrixError:
    """Map an anthropic SDK exception to the matrix exception hierarchy."""
    if isinstance(exc, (AnthropicAuthenticationError, AnthropicPermissionDeniedError)):
        return AuthenticationError(
            "Anthropic authentication failed",
            status_code=getattr(exc, "status_code", 401),
            cause=exc,
        )
    if isinstance(exc, AnthropicRateLimitError):
        return RateLimitError(
            "Anthropic rate limit exceeded",
            status_code=getattr(exc, "status_code", 429),
            cause=exc,
        )
    if isinstance(exc, (
        AnthropicBadRequestError,
        AnthropicNotFoundError,
        AnthropicUnprocessableEntityError,
        AnthropicConflictError,
    )):
        return BadRequestError(
            getattr(exc, "message", str(exc)) or "Anthropic rejected the request",
            status_code=getattr(exc, "status_code", 400),
            code=getattr(exc, "code", None),
            cause=exc,
        )
    if isinstance(exc, AnthropicInternalServerError):
        return ServerError(
            "Anthropic server error",
            status_code=getattr(exc, "status_code", 500),
            cause=exc,
        )
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        if status is not None and status >= 500:
            return ServerError(
                f"Anthropic server error ({status})",
                status_code=status,
                cause=exc,
            )
        return ProviderError(
            getattr(exc, "message", str(exc)) or f"Anthropic status {status}",
            status_code=status,
            cause=exc,
        )
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return NetworkError(
            f"Anthropic network failure: {type(exc).__name__}",
            cause=exc,
        )
    return ProviderError(str(exc), cause=exc)
