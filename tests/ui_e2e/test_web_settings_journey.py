"""Platform › Web in a browser (platform revamp 04, Task 5): where web searches and page fetches go, and the fetchers.

* ``test_web_search_goes_to_an_ordered_list_and_back``: a second search provider is made over REST; Aggregated stores ``["DuckDuckGo"]``, ticking the new row and
  pressing its up button store ``[new, "DuckDuckGo"]`` with a "Web search updated" toast, Single stores the first id, and Aggregated again brings back the list the page
  last saw.
* ``test_plus_fetcher_offers_only_the_remote_fetchers_and_local_has_no_delete``: "+ Fetcher" opens the Register overlay locked to Web Fetch, whose Type offers Jina,
  Firecrawl and Exa and never Local; the built-in ``local`` card opens read-only, with no Delete.
* ``test_a_fetcher_registered_with_plus_fetcher_can_be_the_default``: a keyless Jina fetcher registered through the overlay gets its card ("Jina Reader", "No key
  (rate-limited)"), and picking it as the default stores single mode on it and marks its card "Default fetcher".
* ``test_open_web_settings_from_the_palette``: the palette verb opens the page and writes ``view=platform:web``.
* ``test_the_web_page_on_a_phone``: the phone's Platform section shows both settings with no horizontal overflow.

REST setup and checks go through ``httpx`` (the lane runs with auth disabled, where the middleware's synthetic admin is the caller). Every test puts both settings back to the
built-in single provider and deletes what it made, whatever happens.
"""

from __future__ import annotations

import re
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import default_wid, open_mobile_platform_nav, open_view, run_verb

pytestmark = pytest.mark.ui_e2e

SEARCH_ACTIVE = "/v1/web_search_active_config"
FETCH_ACTIVE = "/v1/web_fetch_active_config"
SEARCH_BUILT_IN = {"mode": "single", "provider_id": "DuckDuckGo"}
FETCH_BUILT_IN = {"mode": "single", "provider_id": "local"}


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=30.0)


def _put(base_url: str, path: str, config: dict) -> None:
    with _client(base_url) as c:
        r = c.put(path, json={"config": config})
        assert r.status_code == 200, f"PUT {path}: {r.status_code} {r.text}"


def _restore(base_url: str, *, search_ids: tuple[str, ...] = (), fetch_ids: tuple[str, ...] = ()) -> None:
    """Both settings back to the built-in single provider, then the rows a test made deleted (a row the active config names cannot be deleted)."""
    with _client(base_url) as c:
        c.put(SEARCH_ACTIVE, json={"config": SEARCH_BUILT_IN})
        c.put(FETCH_ACTIVE, json={"config": FETCH_BUILT_IN})
        for pid in search_ids:
            c.delete(f"/v1/web_search_providers/{pid}")
        for pid in fetch_ids:
            c.delete(f"/v1/web_fetch_providers/{pid}")


def _wait_config(base_url: str, path: str, want: dict, *, timeout_s: float = 15.0) -> None:
    """The stored config reaches ``want`` (each change is one PUT, answered before the page's toast)."""
    deadline = time.monotonic() + timeout_s
    last = None
    with _client(base_url) as c:
        while time.monotonic() < deadline:
            r = c.get(path)
            last = r.json().get("config") if r.status_code == 200 else f"{r.status_code} {r.text}"
            if last == want:
                return
            time.sleep(0.25)
    raise AssertionError(f"GET {path} answers {last!r}, wanted {want!r}")


def _open_web(page: Page, console_url: str) -> None:
    """Assign ``view=platform:web`` once the shell is up (a hash set while it mounts can be replaced by its own normalisation), and wait for both settings to load."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id("web-page")
    for attempt in (1, 2):
        open_view(page, console_url, default_wid(console_url), "platform:web")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            break
        except AssertionError:
            if attempt == 2:
                raise
    expect(page.get_by_test_id("web-search-section")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("web-fetch-section")).to_be_visible(timeout=20_000)


def test_web_search_goes_to_an_ordered_list_and_back(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    new = f"journey-ddg-{unique_suffix}"
    with _client(base_url) as c:
        r = c.post("/v1/web_search_providers", json={"id": new, "provider_type": "duckduckgo", "config": {"type": "duckduckgo"}})
        assert r.status_code in (200, 201), r.text
    try:
        _put(base_url, SEARCH_ACTIVE, SEARCH_BUILT_IN)
        _open_web(page, console_url)
        section = page.get_by_test_id("web-search-section")
        expect(page.get_by_test_id("web-search-default")).to_have_value("DuckDuckGo")

        section.get_by_role("button", name="Aggregated", exact=True).click()
        _wait_config(base_url, SEARCH_ACTIVE, {"mode": "aggregated", "provider_ids": ["DuckDuckGo"]})
        expect(page.get_by_test_id("web-search-chain")).to_be_visible(timeout=15_000)

        # a click, not check(): the box is controlled by the stored config, so it turns on when the save is answered and the list reloads, not under the click
        section.get_by_role("checkbox", name=f"{new} (DuckDuckGo)", exact=True).click()
        _wait_config(base_url, SEARCH_ACTIVE, {"mode": "aggregated", "provider_ids": ["DuckDuckGo", new]})

        page.get_by_test_id(f"web-search-up:{new}").click()
        _wait_config(base_url, SEARCH_ACTIVE, {"mode": "aggregated", "provider_ids": [new, "DuckDuckGo"]})
        expect(page.get_by_test_id("nv-toasts").get_by_text("Web search updated").first).to_be_visible(timeout=10_000)
        expect(page.get_by_test_id(f"web-search-pos:{new}")).to_have_text("1", timeout=15_000)

        section.get_by_role("button", name="Single", exact=True).click()
        _wait_config(base_url, SEARCH_ACTIVE, {"mode": "single", "provider_id": new})
        expect(page.get_by_test_id("web-search-default")).to_have_value(new, timeout=15_000)

        section.get_by_role("button", name="Aggregated", exact=True).click()
        _wait_config(base_url, SEARCH_ACTIVE, {"mode": "aggregated", "provider_ids": [new, "DuckDuckGo"]})
    finally:
        _restore(base_url, search_ids=(new,))


def test_plus_fetcher_offers_only_the_remote_fetchers_and_local_has_no_delete(page: Page, base_url: str, console_url: str) -> None:
    try:
        _put(base_url, FETCH_ACTIVE, FETCH_BUILT_IN)
        _open_web(page, console_url)

        page.get_by_test_id("web-add-fetcher").click()
        overlay = page.get_by_test_id("pc-register")
        expect(overlay).to_be_visible(timeout=15_000)
        selector = overlay.get_by_test_id("pc-register-type")
        expect(selector).to_contain_text("Web Fetch")
        expect(selector).to_have_attribute("aria-disabled", "true")   # one class on offer: the selector is locked on it
        kinds = overlay.get_by_test_id("pc-field:type").locator("option")
        expect(kinds).to_have_count(3, timeout=15_000)
        assert sorted(kinds.all_inner_texts()) == ["Exa", "Firecrawl", "Jina"]
        overlay.get_by_test_id("provider-form-cancel").click()
        expect(overlay).to_have_count(0, timeout=10_000)

        page.get_by_test_id("pk-card-open:local").click()
        expect(overlay).to_be_visible(timeout=15_000)
        expect(overlay).to_contain_text("Edit local")
        expect(overlay.get_by_test_id("provider-form-readonly")).to_be_visible(timeout=15_000)
        expect(overlay.get_by_role("button", name=re.compile("delete", re.I))).to_have_count(0)
        expect(overlay.get_by_test_id("pc-register-delete")).to_have_count(0)
    finally:
        _restore(base_url)


def test_a_fetcher_registered_with_plus_fetcher_can_be_the_default(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    name = f"journey-jina-{unique_suffix}"
    try:
        _put(base_url, FETCH_ACTIVE, FETCH_BUILT_IN)
        _open_web(page, console_url)

        page.get_by_test_id("web-add-fetcher").click()
        overlay = page.get_by_test_id("pc-register")
        expect(overlay).to_be_visible(timeout=15_000)
        overlay.get_by_test_id("pc-field:type").select_option("jina")
        overlay.get_by_test_id("pc-field:id").fill(name)
        save = overlay.get_by_test_id("provider-form-save")
        expect(save).to_be_enabled(timeout=15_000)
        save.click()
        expect(overlay).to_have_count(0, timeout=15_000)

        card = page.get_by_test_id(f"pk-card:{name}")
        expect(card).to_be_visible(timeout=15_000)
        expect(card).to_contain_text("Jina Reader")
        expect(card).to_contain_text("No key (rate-limited)")

        page.get_by_test_id("web-fetch-default").select_option(name)
        _wait_config(base_url, FETCH_ACTIVE, {"mode": "single", "provider_id": name})
        expect(page.get_by_test_id("nv-toasts").get_by_text("Web fetch updated").first).to_be_visible(timeout=10_000)
        expect(card).to_contain_text("Default fetcher", timeout=15_000)
    finally:
        _restore(base_url, fetch_ids=(name,))


def test_open_web_settings_from_the_palette(page: Page, console_url: str) -> None:
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    run_verb(page, "Open Web Settings")
    expect(page.get_by_test_id("web-page")).to_be_visible(timeout=20_000)
    expect(page).to_have_url(re.compile(r"[?&]view=platform:web(&.*)?$"))
    expect(page.get_by_test_id("nv-plat-row:web")).to_have_attribute("aria-current", "page")


def test_the_web_page_on_a_phone(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    frame = open_mobile_platform_nav(page, console_url, "web")
    expect(frame.get_by_test_id("web-page")).to_be_visible(timeout=20_000)
    expect(frame.get_by_test_id("web-search-section")).to_be_visible(timeout=20_000)
    expect(frame.get_by_test_id("web-fetch-section")).to_be_visible(timeout=20_000)
    expect(frame.get_by_test_id("pk-card:local")).to_be_visible(timeout=20_000)
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 2, f"the page is {overflow}px wider than the phone"
