"""Shared openai SDK exception classifier.

Used by every primer adapter that wraps the openai.AsyncOpenAI client
(currently OpenResponsesLLM and OpenAIEmbedder). Maps the openai
exception hierarchy to the primer exception hierarchy so callers see
one universal error surface regardless of which adapter raised.
"""

from __future__ import annotations

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError as OpenAIAuthenticationError,
    BadRequestError as OpenAIBadRequestError,
    InternalServerError as OpenAIInternalServerError,
    PermissionDeniedError as OpenAIPermissionDeniedError,
    RateLimitError as OpenAIRateLimitError,
    UnprocessableEntityError as OpenAIUnprocessableEntityError,
)

from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    PrimerError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
)


def classify_openai_exception(exc: Exception) -> PrimerError:
    """Map an openai SDK exception to the primer exception hierarchy.

    Mapping rules:

    | openai SDK exception | primer exception |
    |---|---|
    | AuthenticationError (401), PermissionDeniedError (403) | AuthenticationError |
    | RateLimitError (429) | RateLimitError |
    | BadRequestError (400) | BadRequestError (carries error code) |
    | UnprocessableEntityError (422) | BadRequestError (carries status 422) |
    | InternalServerError (5xx) | ServerError |
    | Other APIStatusError with status >= 500 | ServerError |
    | Other APIStatusError with a 4xx (402, 404, 409, ...) | BadRequestError (provider code, else ``bad_request``) |
    | Other APIStatusError | ProviderError |
    | APIConnectionError, APITimeoutError | NetworkError |
    | Anything else | ProviderError |

    Used at adapter call sites that wrap the SDK's network/HTTP boundary.
    Callers can either re-raise the result directly, or wrap it into
    whatever shape their interface requires (e.g. a terminal
    ``chat.Error(fatal=True)`` event for the streaming LLM adapter).
    """
    if isinstance(exc, (OpenAIAuthenticationError, OpenAIPermissionDeniedError)):
        return AuthenticationError(
            "OpenAI authentication failed",
            code="auth_error",
            status_code=getattr(exc, "status_code", 401),
            cause=exc,
        )
    if isinstance(exc, OpenAIRateLimitError):
        return RateLimitError(
            "OpenAI rate limit exceeded",
            code="rate_limit",
            status_code=getattr(exc, "status_code", 429),
            cause=exc,
        )
    # A 422 is how an OpenAI-compatible server (vLLM, any FastAPI app)
    # rejects a request that failed its validation: the request was refused,
    # exactly as a 400 is, and the Anthropic classifier already maps it so.
    if isinstance(exc, (OpenAIBadRequestError, OpenAIUnprocessableEntityError)):
        return BadRequestError(
            getattr(exc, "message", str(exc)) or "OpenAI rejected the request",
            status_code=getattr(exc, "status_code", 400),
            code=getattr(exc, "code", None),
            cause=exc,
        )
    if isinstance(exc, OpenAIInternalServerError):
        return ServerError(
            "OpenAI server error",
            code="server_error",
            status_code=getattr(exc, "status_code", 500),
            cause=exc,
        )
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        if status is not None and status >= 500:
            return ServerError(
                f"OpenAI server error ({status})",
                code="server_error",
                status_code=status,
                cause=exc,
            )
        # Any other 4xx is the API refusing this request, as on the Anthropic, Google and Ollama classifiers: a pool
        # fails over on it (a bare ProviderError it does not).
        if status is not None and 400 <= status < 500:
            return BadRequestError(
                getattr(exc, "message", str(exc)) or f"OpenAI rejected the request ({status})",
                status_code=status,
                code=getattr(exc, "code", None) or "bad_request",
                cause=exc,
            )
        return ProviderError(
            getattr(exc, "message", str(exc)) or f"OpenAI status {status}",
            status_code=status,
            cause=exc,
        )
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return NetworkError(
            f"OpenAI network failure: {type(exc).__name__}",
            code="network_error",
            cause=exc,
        )
    return ProviderError(str(exc), cause=exc)
