"""Journey: the triggers DETAIL page says when the server skipped a fire (board ticket 01a12144).

A disabled trigger's fire_now is a 200 ``{skipped: true, fire_id: null, results: []}``; the detail
page's status panel used to print 'Fired' for it. It now reuses the list row's outcome words
(TR_fireOutcome): 'Not fired: <name> is disabled; enable it to fire it.'
"""

from __future__ import annotations

import httpx

import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-03", status="partial")


@pytest.mark.ui_e2e
def test_a_skipped_fire_on_the_detail_page_says_not_fired(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    slug = f"detail-skip-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/triggers", json={"slug": slug, "name": slug, "description": None, "config": {"kind": "webhook"}, "enabled": False})
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
    try:
        open_legacy_route(page, console_url, f"triggers/{tid}")
        expect(page.get_by_test_id("fire-now-btn")).to_be_visible(timeout=45_000)
        page.get_by_test_id("fire-now-btn").click()
        result = page.get_by_test_id("fire-now-result")
        expect(result).to_be_visible(timeout=10_000)
        skipped = page.get_by_test_id("fire-now-skipped")
        expect(skipped).to_be_visible(timeout=10_000)
        expect(skipped).to_contain_text("Not fired")
        expect(skipped).to_contain_text(f"{slug} is disabled; enable it to fire it.")
        expect(result).not_to_contain_text("Fired")
        expect(result).not_to_contain_text("fire id")
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/triggers/{tid}")
