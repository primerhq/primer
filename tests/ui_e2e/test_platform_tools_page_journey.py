"""The Tools page loads on open and on Refresh, not every 15 seconds, and its filter finds a toolset's tools by the toolset's id (Platform revamp Plan 01, Task 11; design 13.5).

``GET /v1/tools/catalogue`` lists every toolset live (ten seconds each at worst), and the page used to ask for it again every fifteen seconds for as long as it stayed open. The journeys watch the
requests the browser makes:

* after the catalogue has loaded, no second ``GET /v1/tools/catalogue`` follows within 20 seconds; the Refresh button makes exactly one more (the unit tests, ``tests/ui/test_platform_tools_page.py``,
  hold what it says while that request is on its way: a browser cannot hold a response open long enough to look);
* a filter on a toolset's id (``<toolset>__``) keeps that toolset's tools on the page and leaves the other toolsets' tools off it.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._platform_tools_helpers import SLOW, open_tools_page

pytestmark = smk("SMK-UI-03", status="partial")

CATALOGUE = "/v1/tools/catalogue"


def test_the_catalogue_is_asked_for_once_on_open_and_again_only_on_refresh(page: Page, console_url: str) -> None:
    catalogue_gets: list[str] = []
    page.on("request", lambda r: catalogue_gets.append(r.url) if r.method == "GET" and CATALOGUE in r.url else None)
    open_tools_page(page, console_url)
    expect(page.locator("[data-testid^='pk-card:']").first).to_be_visible(timeout=SLOW)
    refresh = page.get_by_test_id("nv-plat-refresh")
    expect(refresh).to_have_text("Refresh", timeout=SLOW)
    baseline = len(catalogue_gets)
    assert baseline >= 1, "the page never asked for the catalogue"

    page.wait_for_timeout(20_000)                      # the page used to ask again after 15 s
    assert len(catalogue_gets) == baseline, f"the Tools page polled: {len(catalogue_gets) - baseline} more request(s) within 20 s, {catalogue_gets[baseline:]}"

    refresh.click()
    expect(refresh).to_have_text("Refresh", timeout=SLOW)        # back to its idle word once the load has ended
    expect(refresh).not_to_have_attribute("aria-busy", "true")
    assert len(catalogue_gets) == baseline + 1, f"one Refresh should make exactly one request: {catalogue_gets[baseline:]}"


def test_filtering_by_a_toolset_id_keeps_that_toolsets_tools_and_drops_the_others(page: Page, base_url: str, console_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=120.0) as c:
        r = c.get(CATALOGUE)
        assert r.status_code == 200, r.text
        items = [t for t in r.json()["items"] if "__" in str(t.get("id", ""))]
    by_toolset: dict[str, list[str]] = {}
    for tool in items:
        by_toolset.setdefault(tool["id"].split("__", 1)[0], []).append(tool["id"])
    # a toolset with a few tools, so all of them fit on the first page of matches
    candidates = [ts for ts, ids in by_toolset.items() if 1 <= len(ids) <= 8]
    assert candidates, f"no toolset with between 1 and 8 tools on this install: {sorted((k, len(v)) for k, v in by_toolset.items())}"
    chosen = sorted(candidates)[0]
    needle = f"{chosen}__".lower()
    mine = by_toolset[chosen]

    def says_needle(tool: dict) -> bool:
        """What the page's filter reads of a tool: its bare name, the first line of its description and its scoped id."""
        scoped = tool["id"]
        return needle in " ".join([scoped.split("__", 1)[1], str(tool.get("description") or "").split("\n")[0], scoped]).lower()

    others = [t["id"] for t in items if t["id"].split("__", 1)[0] != chosen and not says_needle(t)]

    open_tools_page(page, console_url)
    expect(page.locator("[data-testid^='pk-card:']").first).to_be_visible(timeout=SLOW)
    page.get_by_test_id("nv-plat-filter").fill(f"{chosen}__")
    for scoped in mine:
        expect(page.get_by_test_id(f"pk-card:{scoped}")).to_be_visible(timeout=10_000)
    assert others, "the install has only one toolset with tools, so there is nothing to leave out"
    for scoped in others[:5]:
        expect(page.get_by_test_id(f"pk-card:{scoped}")).to_have_count(0)
