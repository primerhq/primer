import pytest

from primer.model.except_ import NotFoundError
from primer.model.web_fetch import (
    ACTIVE_WEB_FETCH_CONFIG_ID, ActiveWebFetchConfig,
    AggregatedFetchConfig, SingleFetchConfig,
)
from primer.web_fetch.adapter import (
    DEFAULT_MAX_CHARS, FetchedPage, WebFetchAdapter, WebFetchUnavailable,
)
from primer.web_fetch.adapter import WebFetchProviderError
from primer.web_fetch.service import WebFetchService, _apply_limit


def _page(url="https://x", md="body", thin=False):
    return FetchedPage(final_url=url, content_markdown=md, content_type="text/html",
                       status=200, is_thin=thin)


class _Adapter(WebFetchAdapter):
    def __init__(self, plan):
        self._plan = list(plan); self.calls = 0
    async def fetch(self, *, url):
        self.calls += 1
        nxt = self._plan.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


class _Registry:
    def __init__(self, adapters): self._a = adapters
    async def get(self, pid):
        if pid not in self._a:
            raise NotFoundError(pid)
        return self._a[pid]


class _ConfigStore:
    def __init__(self, row): self.row = row; self.get_calls = 0
    async def get(self, _id): self.get_calls += 1; return self.row


def _single(pid): return ActiveWebFetchConfig(id=ACTIVE_WEB_FETCH_CONFIG_ID, config=SingleFetchConfig(provider_id=pid))
def _agg(pids): return ActiveWebFetchConfig(id=ACTIVE_WEB_FETCH_CONFIG_ID, config=AggregatedFetchConfig(provider_ids=pids))


@pytest.mark.asyncio
async def test_single_mode_returns_page():
    a = _Adapter([_page(md="hello")])
    svc = WebFetchService(registry=_Registry({"a": a}), active_config_storage=_ConfigStore(_single("a")))
    page = await svc.fetch(url="https://x", max_chars=None, max_lines=None)
    assert page.content_markdown == "hello"


@pytest.mark.asyncio
async def test_aggregated_escalates_on_thin():
    a = _Adapter([_page(md="x", thin=True)])
    b = _Adapter([_page(md="full content from b")])
    svc = WebFetchService(registry=_Registry({"a": a, "b": b}), active_config_storage=_ConfigStore(_agg(["a", "b"])))
    page = await svc.fetch(url="https://x", max_chars=None, max_lines=None)
    assert page.content_markdown == "full content from b"
    assert a.calls == 1 and b.calls == 1


@pytest.mark.asyncio
async def test_aggregated_last_thin_returned():
    a = _Adapter([_page(md="thin", thin=True)])
    svc = WebFetchService(registry=_Registry({"a": a}), active_config_storage=_ConfigStore(_agg(["a"])))
    page = await svc.fetch(url="https://x", max_chars=None, max_lines=None)
    assert page.content_markdown == "thin"


@pytest.mark.asyncio
async def test_aggregated_falls_back_on_unavailable():
    a = _Adapter([WebFetchUnavailable("down")])
    b = _Adapter([_page(md="from b")])
    svc = WebFetchService(registry=_Registry({"a": a, "b": b}), active_config_storage=_ConfigStore(_agg(["a", "b"])))
    page = await svc.fetch(url="https://x", max_chars=None, max_lines=None)
    assert page.content_markdown == "from b"


@pytest.mark.asyncio
async def test_default_ceiling_applied_when_unbounded():
    big = "y" * (DEFAULT_MAX_CHARS + 500)
    a = _Adapter([_page(md=big)])
    svc = WebFetchService(registry=_Registry({"a": a}), active_config_storage=_ConfigStore(_single("a")))
    page = await svc.fetch(url="https://x", max_chars=None, max_lines=None)
    assert len(page.content_markdown) == DEFAULT_MAX_CHARS
    assert page.truncated_by_limit is True


@pytest.mark.asyncio
async def test_max_lines_truncates():
    a = _Adapter([_page(md="l1\nl2\nl3\nl4")])
    svc = WebFetchService(registry=_Registry({"a": a}), active_config_storage=_ConfigStore(_single("a")))
    page = await svc.fetch(url="https://x", max_chars=None, max_lines=2)
    assert page.content_markdown == "l1\nl2"
    assert page.truncated_by_limit is True


@pytest.mark.asyncio
async def test_config_cache_ttl():
    a = _Adapter([_page(), _page()])
    store = _ConfigStore(_single("a"))
    svc = WebFetchService(registry=_Registry({"a": a}), active_config_storage=store, cache_ttl_seconds=10.0)
    await svc.fetch(url="https://x", max_chars=None, max_lines=None)
    await svc.fetch(url="https://x", max_chars=None, max_lines=None)
    assert store.get_calls == 1


@pytest.mark.asyncio
async def test_aggregated_all_fail_raises():
    a = _Adapter([WebFetchUnavailable("a down")])
    b = _Adapter([WebFetchUnavailable("b down")])
    svc = WebFetchService(registry=_Registry({"a": a, "b": b}),
                          active_config_storage=_ConfigStore(_agg(["a", "b"])))
    with pytest.raises(WebFetchUnavailable) as exc:
        await svc.fetch(url="https://x", max_chars=None, max_lines=None)
    assert "all 2 providers failed" in str(exc.value)


@pytest.mark.asyncio
async def test_thin_then_unavailable_returns_thin_best_effort():
    a = _Adapter([_page(md="thin a", thin=True)])
    b = _Adapter([WebFetchUnavailable("b down")])
    svc = WebFetchService(registry=_Registry({"a": a, "b": b}),
                          active_config_storage=_ConfigStore(_agg(["a", "b"])))
    page = await svc.fetch(url="https://x", max_chars=None, max_lines=None)
    assert page.content_markdown == "thin a"  # best effort, not a raise


@pytest.mark.asyncio
async def test_no_active_config_raises_provider_error():
    svc = WebFetchService(registry=_Registry({}), active_config_storage=_ConfigStore(None))
    with pytest.raises(WebFetchProviderError):
        await svc.fetch(url="https://x", max_chars=None, max_lines=None)


@pytest.mark.asyncio
async def test_invalidate_forces_config_reread():
    a = _Adapter([_page(), _page()])
    store = _ConfigStore(_single("a"))
    svc = WebFetchService(registry=_Registry({"a": a}),
                          active_config_storage=store, cache_ttl_seconds=100.0)
    await svc.fetch(url="https://x", max_chars=None, max_lines=None)
    svc.invalidate_active_config()
    await svc.fetch(url="https://x", max_chars=None, max_lines=None)
    assert store.get_calls == 2


def test_apply_limit_pure():
    # both None -> ceiling, only truncates when over
    assert _apply_limit("short", None, None) == ("short", False)
    big = "z" * (DEFAULT_MAX_CHARS + 10)
    out, trunc = _apply_limit(big, None, None)
    assert len(out) == DEFAULT_MAX_CHARS and trunc is True
    # exact at char limit -> not truncated
    assert _apply_limit("abcd", 4, None) == ("abcd", False)
    # max_lines then max_chars
    assert _apply_limit("l1\nl2\nl3", None, 2) == ("l1\nl2", True)
    # char cap after line cap
    out2, trunc2 = _apply_limit("aaaa\nbbbb\ncccc", 6, 2)
    assert out2 == "aaaa\nb" and trunc2 is True
