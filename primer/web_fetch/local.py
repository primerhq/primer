"""LocalAdapter: in-process fetch + main-content extraction.

httpx GET (following redirects) -> content-type routing:
  text/html  -> trafilatura markdown (sets is_thin when extraction is short)
  application/pdf -> docling markdown
  application/json -> pretty-printed + fenced
  text/*     -> returned as-is
  other      -> WebFetchProviderError (use http-request for raw bytes)
"""

from __future__ import annotations

import json
import logging

import httpx

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
    """Convert PDF bytes to markdown via docling. Module-level so tests can
    monkeypatch it (docling is heavy and downloads models on first use)."""
    from primer.ingest.loaders.docling import DoclingLoader

    loaded = await DoclingLoader().load(data)
    return loaded.text


class LocalAdapter(WebFetchAdapter):
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        raw_byte_cap: int = DEFAULT_RAW_BYTE_CAP,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._raw_byte_cap = raw_byte_cap
        self._timeout = timeout
        self._user_agent = user_agent

    async def fetch(self, *, url: str) -> FetchedPage:
        try:
            r = await self._client.get(
                url,
                follow_redirects=True,
                timeout=self._timeout,
                headers={"User-Agent": self._user_agent},
            )
        except httpx.HTTPError as exc:
            raise WebFetchUnavailable(
                f"local transport: {type(exc).__name__}: {exc}"
            ) from exc

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

        raw = (r.content or b"")[: self._raw_byte_cap]
        ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
        final_url = str(r.url)

        if ct in ("text/html", "application/xhtml+xml", ""):
            return self._extract_html(raw, ct, final_url, r.status_code)
        if ct == "application/pdf":
            md = await _extract_pdf(raw)
            return FetchedPage(
                final_url=final_url, title="", content_markdown=md,
                content_type=ct, status=r.status_code,
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
                content_type=ct, status=r.status_code,
            )
        if ct.startswith("text/"):
            return FetchedPage(
                final_url=final_url, title="",
                content_markdown=raw.decode("utf-8", errors="replace"),
                content_type=ct, status=r.status_code,
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
