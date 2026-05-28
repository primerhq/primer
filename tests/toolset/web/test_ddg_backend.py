"""Unit tests for primer.toolset.web.backends.ddg.DuckDuckGoBackend."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from primer.model.except_ import ProviderError
from primer.toolset.web.backends.ddg import DuckDuckGoBackend


def _patch_ddgs(scripted_results):
    """Patch ``ddgs.DDGS`` so its context manager returns a fake client.

    Returns a tuple ``(context_manager, fake_client, fake_class)``.
    Use the context manager in a ``with`` block; assert against the
    fake client / class afterwards.
    """
    fake_client = MagicMock()
    if isinstance(scripted_results, Exception):
        fake_client.text.side_effect = scripted_results
    else:
        fake_client.text.return_value = list(scripted_results)
    fake_class = MagicMock()
    fake_class.return_value.__enter__.return_value = fake_client
    fake_class.return_value.__exit__.return_value = False
    return patch("ddgs.DDGS", fake_class), fake_client, fake_class


class TestSearch:
    @pytest.mark.asyncio
    async def test_returns_normalised_hits_in_input_order(self) -> None:
        results = [
            {"title": "Paris", "href": "https://example.com/paris", "body": "City of light."},
            {"title": "Berlin", "href": "https://example.com/berlin", "body": "German capital."},
        ]
        cm, fake_client, _ = _patch_ddgs(results)
        with cm:
            backend = DuckDuckGoBackend()
            hits = await backend.search(
                query="capital of france", count=5, safe_search="moderate"
            )
        assert len(hits) == 2
        assert hits[0].title == "Paris"
        assert hits[0].url == "https://example.com/paris"
        assert hits[0].snippet == "City of light."
        assert hits[1].title == "Berlin"
        # text() received the query positionally, plus translated kwargs.
        call = fake_client.text.call_args
        assert call.args[0] == "capital of france"
        assert call.kwargs["max_results"] == 5
        assert call.kwargs["safesearch"] == "moderate"
        assert call.kwargs["region"] == "us-en"

    @pytest.mark.asyncio
    async def test_translates_safe_search_strict_to_on(self) -> None:
        cm, fake_client, _ = _patch_ddgs([])
        with cm:
            backend = DuckDuckGoBackend()
            await backend.search(query="x", count=3, safe_search="strict")
        # "strict" -> "on" per DDG vocabulary.
        assert fake_client.text.call_args.kwargs["safesearch"] == "on"

    @pytest.mark.asyncio
    async def test_translates_safe_search_off(self) -> None:
        cm, fake_client, _ = _patch_ddgs([])
        with cm:
            backend = DuckDuckGoBackend()
            await backend.search(query="x", count=3, safe_search="off")
        assert fake_client.text.call_args.kwargs["safesearch"] == "off"

    @pytest.mark.asyncio
    async def test_count_zero_short_circuits(self) -> None:
        cm, fake_client, fake_class = _patch_ddgs([])
        with cm:
            backend = DuckDuckGoBackend()
            hits = await backend.search(query="x", count=0, safe_search="moderate")
        assert hits == []
        # No DDGS construction or text() call when there's nothing to ask for.
        fake_class.assert_not_called()
        fake_client.text.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_query_raises_provider_error(self) -> None:
        backend = DuckDuckGoBackend()
        with pytest.raises(ProviderError, match="non-empty"):
            await backend.search(query="", count=5, safe_search="moderate")

    @pytest.mark.asyncio
    async def test_ddg_exception_translated_to_provider_error(self) -> None:
        cm, _, _ = _patch_ddgs(RuntimeError("rate limited"))
        with cm:
            backend = DuckDuckGoBackend()
            with pytest.raises(ProviderError, match="rate limited"):
                await backend.search(
                    query="x", count=3, safe_search="moderate"
                )

    @pytest.mark.asyncio
    async def test_handles_legacy_url_snippet_keys(self) -> None:
        # Older library versions occasionally use ``url`` / ``snippet`` instead.
        results = [
            {"title": "Old Format", "url": "https://e/x", "snippet": "old shape"},
        ]
        cm, _, _ = _patch_ddgs(results)
        with cm:
            backend = DuckDuckGoBackend()
            hits = await backend.search(query="x", count=1, safe_search="moderate")
        assert hits[0].url == "https://e/x"
        assert hits[0].snippet == "old shape"

    @pytest.mark.asyncio
    async def test_missing_keys_default_to_empty_strings(self) -> None:
        cm, _, _ = _patch_ddgs([{}])
        with cm:
            backend = DuckDuckGoBackend()
            hits = await backend.search(query="x", count=1, safe_search="moderate")
        assert hits[0].title == ""
        assert hits[0].url == ""
        assert hits[0].snippet == ""

    @pytest.mark.asyncio
    async def test_custom_region_forwarded(self) -> None:
        cm, fake_client, _ = _patch_ddgs([])
        with cm:
            backend = DuckDuckGoBackend(region="fr-fr")
            await backend.search(query="x", count=3, safe_search="moderate")
        assert fake_client.text.call_args.kwargs["region"] == "fr-fr"
