"""Tests for the WebSearchAdapter ABC + named exceptions + SearchHit."""

from __future__ import annotations

import pytest

from primer.model.except_ import PrimerError
from primer.web_search.adapter import (
    SearchHit,
    WebSearchAdapter,
    WebSearchProviderError,
    WebSearchUnavailable,
)


class TestSearchHit:
    def test_round_trip(self) -> None:
        hit = SearchHit(title="Primer", url="https://example/", snippet="X")
        dumped = hit.model_dump()
        assert dumped == {
            "title": "Primer",
            "url": "https://example/",
            "snippet": "X",
        }

    def test_snippet_defaults_to_empty(self) -> None:
        hit = SearchHit(title="t", url="https://u/")
        assert hit.snippet == ""


class TestNamedExceptions:
    def test_unavailable_inherits_primer_error(self) -> None:
        exc = WebSearchUnavailable("rate-limited")
        assert isinstance(exc, PrimerError)
        assert str(exc) == "rate-limited"

    def test_provider_error_inherits_primer_error(self) -> None:
        exc = WebSearchProviderError("auth failed")
        assert isinstance(exc, PrimerError)
        assert str(exc) == "auth failed"

    def test_distinct_classes(self) -> None:
        # Critical: the service catches these two by name, not by parent.
        assert WebSearchUnavailable is not WebSearchProviderError


class TestAbstractBase:
    def test_cannot_instantiate_abstract_base(self) -> None:
        with pytest.raises(TypeError):
            WebSearchAdapter()  # type: ignore[abstract]

    @pytest.mark.asyncio
    async def test_default_aclose_is_no_op(self) -> None:
        class _Concrete(WebSearchAdapter):
            async def search(self, *, query, count, safe_search):
                return []

        adapter = _Concrete()
        await adapter.aclose()  # no exception
