import httpx
import pytest

from primer.web_fetch.adapter import WebFetchProviderError, WebFetchUnavailable
from primer.web_fetch.local import LocalAdapter

HTML = (
    "<html><head><title>My Title</title></head><body>"
    "<nav>nav junk</nav>"
    "<article><h1>Heading</h1>"
    "<p>" + ("This is the real article body. " * 20) + "</p></article>"
    "</body></html>"
)


def _adapter(handler) -> LocalAdapter:
    return LocalAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_html_extracts_markdown_and_title():
    def h(req): return httpx.Response(200, headers={"content-type": "text/html"}, text=HTML)
    page = await _adapter(h).fetch(url="https://example.com/a")
    assert "real article body" in page.content_markdown
    assert "nav junk" not in page.content_markdown
    assert page.title == "My Title"
    assert page.is_thin is False
    assert page.content_type == "text/html"


@pytest.mark.asyncio
async def test_thin_html_marks_is_thin_not_raises():
    def h(req): return httpx.Response(200, headers={"content-type": "text/html"},
                                      text="<html><body><div id=root></div></body></html>")
    page = await _adapter(h).fetch(url="https://spa.example.com")
    assert page.is_thin is True


@pytest.mark.asyncio
async def test_json_passthrough_fenced():
    def h(req): return httpx.Response(200, headers={"content-type": "application/json"},
                                      text='{"a":1}')
    page = await _adapter(h).fetch(url="https://api.example.com/x")
    assert page.content_markdown.startswith("```json")
    assert '"a": 1' in page.content_markdown


@pytest.mark.asyncio
async def test_plain_text_passthrough():
    def h(req): return httpx.Response(200, headers={"content-type": "text/plain"}, text="hello")
    page = await _adapter(h).fetch(url="https://example.com/robots.txt")
    assert page.content_markdown == "hello"


@pytest.mark.asyncio
async def test_unsupported_binary_raises_provider_error():
    def h(req): return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
    with pytest.raises(WebFetchProviderError):
        await _adapter(h).fetch(url="https://example.com/x.png")


@pytest.mark.asyncio
async def test_pdf_uses_injected_extractor(monkeypatch):
    import primer.web_fetch.local as mod
    async def _fake(data): return "PDF AS MARKDOWN"
    monkeypatch.setattr(mod, "_extract_pdf", _fake)
    def h(req): return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4")
    page = await _adapter(h).fetch(url="https://example.com/x.pdf")
    assert page.content_markdown == "PDF AS MARKDOWN"
    assert page.content_type == "application/pdf"


@pytest.mark.asyncio
async def test_429_raises_unavailable():
    def h(req): return httpx.Response(429, text="slow down")
    with pytest.raises(WebFetchUnavailable):
        await _adapter(h).fetch(url="https://example.com")


@pytest.mark.asyncio
async def test_transport_error_raises_unavailable():
    def h(req): raise httpx.ConnectError("refused")
    with pytest.raises(WebFetchUnavailable):
        await _adapter(h).fetch(url="https://example.com")


@pytest.mark.asyncio
async def test_sends_browser_user_agent_not_default_httpx():
    """Many sites (Wikipedia, Cloudflare-fronted hosts) 403 the default
    ``python-httpx`` User-Agent. The local adapter must present a
    browser-like UA so real web pages are fetchable."""
    seen: dict[str, str] = {}

    def h(req):
        seen["ua"] = req.headers.get("user-agent", "")
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")

    await _adapter(h).fetch(url="https://en.wikipedia.org/wiki/Foo")
    assert "python-httpx" not in seen["ua"].lower()
    assert "mozilla" in seen["ua"].lower()


@pytest.mark.asyncio
async def test_user_agent_is_overridable():
    """A caller may pin a custom UA (e.g. an honest bot identity)."""
    seen: dict[str, str] = {}

    def h(req):
        seen["ua"] = req.headers.get("user-agent", "")
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")

    adapter = LocalAdapter(
        client=httpx.AsyncClient(transport=httpx.MockTransport(h)),
        user_agent="PrimerBot/1.0 (+https://primer.example)",
    )
    await adapter.fetch(url="https://example.com")
    assert seen["ua"] == "PrimerBot/1.0 (+https://primer.example)"
