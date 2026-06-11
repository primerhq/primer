from primer.web_fetch.adapter import (
    DEFAULT_MAX_CHARS,
    THIN_CONTENT_THRESHOLD,
    FetchedPage,
    WebFetchProviderError,
    WebFetchUnavailable,
)


def test_fetched_page_defaults():
    p = FetchedPage(
        final_url="https://x", content_markdown="hi", content_type="text/html",
        status=200,
    )
    assert p.title == ""
    assert p.is_thin is False
    assert p.truncated_by_limit is False


def test_exceptions_are_distinct():
    assert not issubclass(WebFetchProviderError, WebFetchUnavailable)
    assert DEFAULT_MAX_CHARS == 100 * 1024
    assert THIN_CONTENT_THRESHOLD == 200
