"""Shared google-genai SDK exception classifier.

Used by every matrix adapter that wraps the google-genai client
(currently GeminiLLM; the Gemini Embedder sub-project will share
this when it ships). Maps the google-genai exception hierarchy to
the matrix exception hierarchy so callers see one universal error
surface regardless of which adapter raised.

Dispatches primarily on the HTTP status code carried by
``google.genai.errors.APIError.code`` rather than on subclass identity
(google-genai only distinguishes 4xx ``ClientError`` vs 5xx
``ServerError`` via subclass; the granular semantic distinctions
matrix cares about — auth vs rate-limit vs bad-request — live in the
HTTP code).

Network failures from the underlying httpx transport (``TimeoutException``,
``NetworkError``, ``ConnectError``) are caught and mapped to
``matrix.NetworkError``.
"""

from __future__ import annotations

import httpx
from google.genai import errors as gerrors

from matrix.model.except_ import (
    AuthenticationError,
    BadRequestError,
    MatrixError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
)


def classify_google_exception(exc: Exception) -> MatrixError:
    """Map a google-genai SDK exception to the matrix exception hierarchy.

    Mapping rules:

    | google-genai exception | matrix exception |
    |---|---|
    | ``APIError`` with code in {401, 403} | ``AuthenticationError`` |
    | ``APIError`` with code == 429 | ``RateLimitError`` |
    | ``APIError`` with code in 4xx (other) | ``BadRequestError`` |
    | ``APIError`` with code in 5xx | ``ServerError`` |
    | Any other ``APIError`` | ``ProviderError`` |
    | ``httpx.TimeoutException`` / ``httpx.NetworkError`` | ``NetworkError`` |
    | Anything else | ``ProviderError`` |
    """
    if isinstance(exc, gerrors.APIError):
        code = getattr(exc, "code", None) or 0
        message = getattr(exc, "message", None) or str(exc)
        if code in (401, 403):
            return AuthenticationError(
                "Google authentication failed",
                status_code=code,
                cause=exc,
            )
        if code == 429:
            return RateLimitError(
                "Google rate limit exceeded",
                status_code=code,
                cause=exc,
            )
        if 400 <= code < 500:
            return BadRequestError(
                message or "Google rejected the request",
                status_code=code,
                cause=exc,
            )
        if code >= 500:
            return ServerError(
                f"Google server error ({code})",
                status_code=code,
                cause=exc,
            )
        return ProviderError(
            message or f"Google API error: {type(exc).__name__}",
            status_code=code if code else None,
            cause=exc,
        )
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return NetworkError(
            f"Google network failure: {type(exc).__name__}",
            cause=exc,
        )
    return ProviderError(str(exc), cause=exc)
