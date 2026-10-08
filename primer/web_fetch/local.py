"""LocalAdapter: in-process fetch + main-content extraction.

httpx GET (following redirects; every hop passes the egress guard in
primer.common.netguard) -> content-type routing:
  text/html  -> trafilatura markdown (sets is_thin when extraction is short)
  application/pdf -> unsupported (binary conversion removed in v2)
  application/json -> pretty-printed + fenced
  text/*     -> returned as-is
  other      -> WebFetchProviderError (use http-request for raw bytes)
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from primer.common.bounded_read import read_capped
from primer.common.netguard import EgressRefused, guarded_async_client

from primer.web_fetch.adapter import (
    THIN_CONTENT_THRESHOLD,
    FetchedPage,
    WebFetchAdapter,
    WebFetchProviderError,
    WebFetchUnavailable,
)


logger = logging.getLogger(__name__)

# Raw-response cap (pre-extraction); larger than http-request's 1 MB to fit PDFs.
DEFAULT_RAW_BYTE_CAP = 5 * 1024 * 1024

# Many hosts (Wikipedia, Cloudflare-fronted sites) reject httpx's default
# ``python-httpx/x.y`` User-Agent with a 403. Present a mainstream browser UA
# so ordinary human-readable pages are fetchable; callers may override.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def _extract_pdf(data: bytes) -> str:
    """PDF extraction was removed in v2 along with the ingest package.

    Kept as the single seam the adapter calls so the failure is one clear
    message rather than an import error deep in a fetch.
    """
    raise UnsupportedContentError(
        "PDF extraction is not supported; fetch a text or HTML URL, or "
        "convert the document before fetching it."
    )


class LocalAdapter(WebFetchAdapter):
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        raw_byte_cap: int = DEFAULT_RAW_BYTE_CAP,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._client = client or guarded_async_client(timeout=timeout)
        self._owns_client = client is None
        self._raw_byte_cap = raw_byte_cap
        self._timeout = timeout
        self._user_agent = user_agent

    async def fetch(self, *, url: str) -> FetchedPage:
        try:
            # One deadline for the request AND the body (the client's timeout is per operation, so a body that drips never trips it), and
            # the body is read only to the cap: ``r.content`` would hold a response that streams gigabytes in memory (architecture review
            # A-10). The status is judged before the body is touched, so an error page is not read at all.
            async with asyncio.timeout(self._timeout):
                async with self._client.stream(
                    "GET",
                    url,
                    follow_redirects=True,
                    timeout=self._timeout,
                    headers={"User-Agent": self._user_agent},
                ) as r:
                    if r.status_code == 429:
                        raise WebFetchUnavailable("local fetch rate-limited (HTTP 429)")
                    if r.status_code >= 500:
                        raise WebFetchUnavailable(f"local fetch server error (HTTP {r.status_code})")
                    if r.status_code in (401, 403):
                        raise WebFetchProviderError(f"local fetch forbidden (HTTP {r.status_code})")
                    if r.status_code >= 400:
                        raise WebFetchProviderError(
                            f"local fetch unexpected status {r.status_code}"
                        )
                    raw, _ = await read_capped(r, self._raw_byte_cap)
                    ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    final_url = str(r.url)
                    status = r.status_code
        except EgressRefused as exc:
            # Not transient: the target is internal. A remote provider in
            # an aggregated chain may still fetch it from its own network.
            raise WebFetchProviderError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise WebFetchUnavailable(
                f"local transport: {type(exc).__name__}: {exc}"
            ) from exc
        except TimeoutError as exc:
            raise WebFetchUnavailable(
                f"local fetch timed out after {self._timeout:g}s"
            ) from exc

        if ct in ("text/html", "application/xhtml+xml", ""):
            return self._extract_html(raw, ct, final_url, status)
        if ct == "application/pdf":
            md = await _extract_pdf(raw)
            return FetchedPage(
                final_url=final_url, title="", content_markdown=md,
                content_type=ct, status=status,
            )
        if ct == "application/json":
            text = raw.decode("utf-8", errors="replace")
            try:
                pretty = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except ValueError:
                pretty = text
            return FetchedPage(
                final_url=final_url, title="",
                content_markdown=f"```json\n{pretty}\n```",
                content_type=ct, status=status,
            )
        if ct.startswith("text/"):
            return FetchedPage(
                final_url=final_url, title="",
                content_markdown=raw.decode("utf-8", errors="replace"),
                content_type=ct, status=status,
            )
        raise WebFetchProviderError(
            f"unsupported content type {ct!r}; use http-request for raw bytes"
        )

    def _extract_html(
        self, raw: bytes, ct: str, final_url: str, status: int,
    ) -> FetchedPage:
        import lxml.html as lh
        import trafilatura

        html = raw.decode("utf-8", errors="replace")
        md = trafilatura.extract(
            html, output_format="markdown",
            include_links=True, include_tables=True, url=final_url,
        )

        # Extract <title> tag directly via lxml for fidelity; trafilatura's
        # extract_metadata may prefer the first heading over the title element.
        title = ""
        try:
            tree = lh.fromstring(raw)
            nodes = tree.xpath("//title/text()")
            if nodes:
                title = nodes[0].strip()
        except Exception:  # noqa: BLE001 -- title is best-effort
            title = ""

        body = md or ""
        is_thin = len(body.strip()) < THIN_CONTENT_THRESHOLD
        return FetchedPage(
            final_url=final_url, title=title,
            content_markdown=body, content_type=ct or "text/html",
            status=status, is_thin=is_thin,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["LocalAdapter"]
