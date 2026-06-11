import pytest

from primer.web_fetch.adapter import FetchedPage, WebFetchUnavailable
from primer.toolset.web.tools import WebFetchArgs, make_web_fetch_handler


class _Service:
    def __init__(self, page=None, exc=None): self._page = page; self._exc = exc; self.kwargs = None
    async def fetch(self, *, url, max_chars, max_lines):
        self.kwargs = {"url": url, "max_chars": max_chars, "max_lines": max_lines}
        if self._exc is not None:
            raise self._exc
        return self._page


@pytest.mark.asyncio
async def test_handler_builds_header_and_output():
    page = FetchedPage(final_url="https://x/a", title="My Page",
                       content_markdown="clean body", content_type="text/html", status=200)
    svc = _Service(page=page)
    res = await make_web_fetch_handler(svc)({"url": "https://x/a"})
    assert res.is_error is False
    assert res.output.startswith("# My Page\nSource: https://x/a\n\nclean body")
    assert res.extended["content_type"] == "text/html"
    assert res.extended["truncated_by_limit"] is False
    assert svc.kwargs == {"url": "https://x/a", "max_chars": None, "max_lines": None}


@pytest.mark.asyncio
async def test_handler_passes_limits():
    page = FetchedPage(final_url="https://x", content_markdown="b", content_type="text/html", status=200)
    svc = _Service(page=page)
    await make_web_fetch_handler(svc)({"url": "https://x", "max_chars": 100, "max_lines": 5})
    assert svc.kwargs["max_chars"] == 100 and svc.kwargs["max_lines"] == 5


@pytest.mark.asyncio
async def test_handler_thin_appends_note():
    page = FetchedPage(final_url="https://x", content_markdown="tiny", content_type="text/html",
                       status=200, is_thin=True)
    res = await make_web_fetch_handler(_Service(page=page))({"url": "https://x"})
    assert "JavaScript" in res.output or "javascript" in res.output.lower()


@pytest.mark.asyncio
async def test_handler_maps_unavailable_to_error():
    res = await make_web_fetch_handler(_Service(exc=WebFetchUnavailable("down")))({"url": "https://x"})
    assert res.is_error is True
    assert "web-fetch failed" in res.output


@pytest.mark.asyncio
async def test_args_reject_bad_limit():
    import pytest as _p
    from pydantic import ValidationError
    with _p.raises(ValidationError):
        WebFetchArgs.model_validate({"url": "https://x", "max_chars": 0})
