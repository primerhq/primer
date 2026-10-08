"""An unconfigured Internal Collections page says so in plain words and loads without a 404 (finding L2 of the 2026-10-08 review).

The page probed ``GET /v1/internal_collections/config`` and the route answered 404 for "not configured": the page read it as "off", but the
browser logged a red ``Failed to load resource`` error on every open, and the card explained itself in API terms ("The four
/v1/{kind}/search routes return 503 until..."). The journey runs against the auth-disabled fresh instance the lane uses, where the
subsystem is not configured, and checks both: the plain sentence is on screen and no request to the config route failed.
"""

from __future__ import annotations

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-01")

_PLAIN = "Semantic search over your agents, graphs, collections and tools is off until you configure it."


def test_the_unconfigured_page_is_plain_and_its_probe_does_not_404(
    page,
    console_url: str,
    base_url: str,
    failed_requests: list[dict],
) -> None:
    import httpx

    # The lane's instance has no Internal Collections config; if a previous test left one, the page cannot be in the state under test.
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        assert c.get("/v1/internal_collections/config", params={"allow_missing": "true"}).json() == {"configured": False}, (
            "this journey needs an unconfigured subsystem"
        )

    open_legacy_route(page, console_url, "subsystems/internal-collections")
    page.get_by_text(_PLAIN).wait_for(state="visible", timeout=15_000)
    # The probe polls; give the first answer time to land before looking at failures.
    page.wait_for_timeout(1_500)

    body = page.locator("body").inner_text()
    assert "/v1/{kind}" not in body and "return 503" not in body, "the card still speaks API"
    probe_failures = [r for r in failed_requests if "/internal_collections/config" in r["url"]]
    assert probe_failures == [], f"the config probe failed: {probe_failures}"
