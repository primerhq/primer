"""Error mapping shared by the vendor token counters."""

from __future__ import annotations

from primer.model.except_ import BadRequestError, PrimerError, ProviderError


def promote_unclassified_4xx(error: PrimerError) -> PrimerError:
    """A bare ``ProviderError`` carrying a 4xx status is a rejected request.

    The SDK classifiers fall through to ``ProviderError`` for a status they have
    no class for (413, 422, 402...). To the counter wrapper a bare ``ProviderError``
    is an unexpected failure (``fallback_bug``, an ERROR with a traceback); a 4xx
    is the request being refused, which is a deterministic rejection.
    """
    status = getattr(error, "status_code", None)
    if type(error) is ProviderError and status is not None and 400 <= status < 500:
        return BadRequestError(
            error.message, code=error.code, status_code=status, cause=error.cause,
        )
    return error


__all__ = ["promote_unclassified_4xx"]
