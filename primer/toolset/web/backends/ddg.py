"""DuckDuckGo backend for the ``web-search`` tool.

Wraps the `ddgs <https://pypi.org/project/ddgs/>`_ package (renamed
from ``duckduckgo-search``). Pure-Python, no API key, no headless
browser. The library is synchronous; we run it inside
:func:`asyncio.to_thread` so the event loop is not blocked.

This is the default backend used by :func:`build_web_toolset`. Brave,
Tavily, Serper, and Exa are future opt-in adapters slotting into the
same :class:`WebSearchBackend` protocol.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from primer.model.except_ import ProviderError
from primer.toolset.web.backends.base import (
    SafeSearchLevel,
    SearchHit,
    WebSearchBackend,
)


logger = logging.getLogger(__name__)


# DDG's library uses ``on`` for the strictest level and ``moderate`` /
# ``off`` for the others; translate from the framework's user-facing
# vocabulary so the public schema stays uniform across backends.
_SAFE_SEARCH_TO_DDG: dict[SafeSearchLevel, str] = {
    "off": "off",
    "moderate": "moderate",
    "strict": "on",
}


class DuckDuckGoBackend(WebSearchBackend):
    """Default :class:`WebSearchBackend`, backed by the `ddgs` library."""

    def __init__(self, *, region: str = "us-en") -> None:
        self._region = region

    async def search(
        self,
        *,
        query: str,
        count: int,
        safe_search: SafeSearchLevel,
    ) -> list[SearchHit]:
        if not query:
            # Defence-in-depth; the WebSearchArgs model already enforces this.
            raise ProviderError("DuckDuckGoBackend: query must be non-empty")
        if count <= 0:
            return []

        ddg_safesearch = _SAFE_SEARCH_TO_DDG[safe_search]
        try:
            raw = await asyncio.to_thread(
                _ddg_text_call,
                query=query,
                max_results=count,
                safesearch=ddg_safesearch,
                region=self._region,
            )
        except Exception as exc:  # noqa: BLE001 -- adapter classifier
            raise _classify_ddg_exception(exc) from exc

        return [_to_hit(item) for item in raw]


# ---- Internals -------------------------------------------------------------


def _ddg_text_call(
    *,
    query: str,
    max_results: int,
    safesearch: str,
    region: str,
) -> list[dict[str, Any]]:
    """Sync wrapper around ``DDGS.text`` for ``asyncio.to_thread``.

    Imported lazily so test modules can patch ``ddgs.DDGS`` without the
    real client being constructed at import time.
    """
    from ddgs import DDGS

    with DDGS() as client:
        return client.text(
            query,
            max_results=max_results,
            safesearch=safesearch,
            region=region,
        )


def _to_hit(item: dict[str, Any]) -> SearchHit:
    """Normalise one DDG result dict into a :class:`SearchHit`.

    DDG result keys are ``title``, ``href``, ``body`` (matching
    :class:`ddgs.results.TextResult`). Older versions occasionally use
    ``url`` or ``snippet`` instead, so accept both spellings.
    """
    title = str(item.get("title") or "")
    url = str(item.get("href") or item.get("url") or "")
    snippet = str(item.get("body") or item.get("snippet") or "")
    return SearchHit(title=title, url=url, snippet=snippet)


def _classify_ddg_exception(exc: BaseException) -> ProviderError:
    """Translate ``ddgs`` errors into the framework's typed exception."""
    return ProviderError(
        f"DuckDuckGoBackend: search failed: {type(exc).__name__}: {exc}",
        cause=exc,
    )


__all__ = ["DuckDuckGoBackend"]
