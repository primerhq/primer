"""Error mapping shared by the vendor token counters."""

from __future__ import annotations

from primer.model.except_ import (
    BadRequestError,
    PrimerError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
)


# 4xx statuses that mean "try again", not "your request is refused". Neither has an
# SDK class, so they arrive as a bare ProviderError (Anthropic) or are filed under
# BadRequestError with the rest of the 4xx (the shared Google classifier).
_RETRY_LATER: dict[int, type[ProviderError]] = {
    408: ProviderTimeoutError,  # the server gave up waiting for the request
    425: RateLimitError,  # too early; the retry-later class (429 itself is one)
}


def promote_unclassified_4xx(error: PrimerError) -> PrimerError:
    """A bare ``ProviderError`` carrying a 4xx status is a rejected request.

    The SDK classifiers fall through to ``ProviderError`` for a status they have
    no class for (413, 422, 402...). To the counter wrapper a bare ``ProviderError``
    is an unexpected failure (``fallback_bug``, an ERROR with a traceback); a 4xx
    is the request being refused, which is a deterministic rejection.

    408 and 425 are the exception: they are not a refusal, they are "try again".
    Left as rejections they would never be negative-cached and every count would
    hit the endpoint again, so they map to the transient classes the wrapper
    caches (``_RETRY_LATER``), whether they arrived bare or already filed under
    ``BadRequestError``. An error of any other class is returned untouched.
    """
    status = getattr(error, "status_code", None)
    if type(error) not in (ProviderError, BadRequestError) or status is None:
        return error
    fields = dict(code=error.code, status_code=status, cause=error.cause)
    retry_later = _RETRY_LATER.get(status)
    if retry_later is not None:
        return retry_later(error.message, **fields)
    if type(error) is ProviderError and 400 <= status < 500:
        return BadRequestError(error.message, **fields)
    return error


__all__ = ["promote_unclassified_4xx"]
