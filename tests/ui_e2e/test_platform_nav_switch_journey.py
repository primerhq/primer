"""Platform: leaving a page for Providers stops that page's polling, and the nav walks every page without a React error (hook-order ticket of the 2026-10-08 admin review).

``NV_PlatPage`` returned the Providers page BEFORE it called its own hooks, so one component instance rendered with no hooks on Providers and with a dozen on every other page.
React raises nothing for that, but the hooks of the page left behind never run again, so their cleanups never run: the list resource of the page the operator just left kept
polling every 15 s for as long as Providers stayed open. Measured on the old code with the real nav: a ``GET /v1/agents?limit=200`` 13.4 s after leaving the Agents page for
Providers (the 15 s poll counted from its last fetch), and none after leaving it for Graphs (the control: two pages with hooks).

* ``test_a_page_left_for_providers_stops_polling_its_list`` waits out one poll interval after leaving Agents for Providers and fails on any further request for the Agents list.
  It takes about 14 s, which is the interval it measures.
* ``test_switching_between_providers_and_the_other_pages_renders_each_and_raises_no_error`` walks Providers -> Toolsets -> Providers -> Agents -> Providers. It passes on the old
  code too (the mismatch is silent), so it is a guard for the render path of the fix, not its red test.
"""

from __future__ import annotations

import time

from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")

POLL_SECONDS = 15


def _open_providers(page: Page, console_url: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own normalisation), so wait for it and
    navigate once more if the Providers page is not there after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id("nv-plat-page:providers")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", "platform:providers")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def _go(page: Page, nav: str) -> None:
    page.get_by_test_id(f"nv-plat-row:{nav}").click()
    expect(page.get_by_test_id(f"nv-plat-page:{nav}")).to_be_visible(timeout=15_000)


def test_a_page_left_for_providers_stops_polling_its_list(page: Page, console_url: str) -> None:
    fetches: list[float] = []
    page.on("request", lambda r: fetches.append(time.time()) if r.method == "GET" and "/v1/agents?limit=200" in r.url else None)
    _open_providers(page, console_url)

    _go(page, "agents")
    deadline = time.time() + 15
    while not fetches and time.time() < deadline:
        page.wait_for_timeout(100)
    assert fetches, "the Agents page never fetched its list"
    last_fetch = fetches[-1]

    _go(page, "providers")
    left = time.time()
    # The poll is POLL_SECONDS from the last fetch: wait past it, with a margin.
    page.wait_for_timeout(max(0, int((last_fetch + POLL_SECONDS + 1.5 - time.time()) * 1000)))

    late = [round(t - left, 1) for t in fetches if t > left]
    assert late == [], f"the Agents list was fetched again {late} s after the page was left for Providers"


def test_switching_between_providers_and_the_other_pages_renders_each_and_raises_no_error(page: Page, console_url: str) -> None:
    raised: list[str] = []
    page.on("pageerror", lambda e: raised.append(str(e)))
    page.on("console", lambda m: raised.append(m.text) if m.type == "error" else None)

    _open_providers(page, console_url)
    for nav in ("toolsets", "providers", "agents", "providers"):
        _go(page, nav)

    hooks = [m for m in raised if "hook" in m.lower()]
    assert hooks == [], f"React reported a hook-order error while switching pages: {hooks[:2]}"
