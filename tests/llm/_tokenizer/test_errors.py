"""promote_unclassified_4xx: which bare ProviderErrors are a refusal, which a retry."""

from __future__ import annotations

import pytest

from primer.llm._tokenizer._errors import promote_unclassified_4xx
from primer.model.except_ import (
    BadRequestError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    ServerError,
)


def _bare(status: int | None) -> ProviderError:
    return ProviderError("boom", code="c", status_code=status, cause=ValueError("root"))


@pytest.mark.parametrize("status", [402, 404, 413, 422])
def test_an_unclassified_4xx_is_a_rejection(status: int) -> None:
    got = promote_unclassified_4xx(_bare(status))
    assert type(got) is BadRequestError
    assert (got.message, got.code, got.status_code) == ("boom", "c", status)
    assert isinstance(got.cause, ValueError)


def test_a_408_is_a_timeout_and_keeps_its_fields() -> None:
    got = promote_unclassified_4xx(_bare(408))
    assert type(got) is ProviderTimeoutError
    assert (got.message, got.code, got.status_code) == ("boom", "c", 408)
    assert isinstance(got.cause, ValueError)


def test_a_425_is_retry_later_and_keeps_its_fields() -> None:
    got = promote_unclassified_4xx(_bare(425))
    assert type(got) is RateLimitError
    assert (got.message, got.code, got.status_code) == ("boom", "c", 425)


@pytest.mark.parametrize(
    ("status", "expected"), [(408, ProviderTimeoutError), (425, RateLimitError)],
)
def test_a_retry_later_status_already_filed_as_bad_request_is_refiled(status, expected) -> None:
    """The shared Google classifier files every other 4xx under BadRequestError, so
    that is the shape 408 and 425 arrive in on the Gemini path."""
    got = promote_unclassified_4xx(BadRequestError("x", code="bad_request", status_code=status))
    assert type(got) is expected
    assert (got.code, got.status_code) == ("bad_request", status)


def test_an_ordinary_bad_request_is_left_alone() -> None:
    error = BadRequestError("x", status_code=422)
    assert promote_unclassified_4xx(error) is error


@pytest.mark.parametrize("status", [None, 200, 301, 500, 503])
def test_anything_that_is_not_a_4xx_is_left_alone(status) -> None:
    error = _bare(status)
    assert promote_unclassified_4xx(error) is error


@pytest.mark.parametrize(
    "error",
    [
        BadRequestError("x", status_code=400),
        RateLimitError("x", status_code=429),
        ServerError("x", status_code=500),
        ProviderTimeoutError("x", status_code=408),
    ],
)
def test_an_already_classified_error_is_never_reclassified(error) -> None:
    assert promote_unclassified_4xx(error) is error
