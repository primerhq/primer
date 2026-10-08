"""Journey: a worker that has not reported its load reads as unknown on the Workers page, not as idle (follow-up to #484).

``GET /v1/workers`` is answered here by the test with one worker that reported (2 of 4 slots) and one that did not (``in_flight: null``),
the shape a worker from before load reporting has. The page must not turn the missing number into "0 / 3" and a confident fleet total.
"""

from __future__ import annotations

import json

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_overlay

_NOW = "2026-10-08T12:00:00+00:00"


def _worker(wid: str, capacity: int, in_flight: int | None) -> dict:
    return {
        "id": wid, "host": "host-" + wid, "pid": 100, "capacity": capacity, "started_at": _NOW, "last_heartbeat": _NOW,
        "status": "active", "in_flight": in_flight,
    }


@pytest.mark.ui_e2e
def test_an_unreported_load_is_unknown_on_the_workers_page(page: Page, base_url: str, console_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        wid = c.get("/v1/workspaces").json()["items"][0]["id"]
    body = json.dumps({"kind": "offset", "offset": 0, "length": 2, "total": 2, "items": [_worker("w-known", 4, 2), _worker("w-unknown", 3, None)]})
    page.route("**/v1/workers?*", lambda route: route.fulfill(status=200, content_type="application/json", body=body))
    page.route("**/v1/workers", lambda route: route.fulfill(status=200, content_type="application/json", body=body))

    open_overlay(page, console_url, wid, "workers")
    rows = page.get_by_test_id("worker-row")
    expect(rows).to_have_count(2, timeout=20_000)
    expect(rows.filter(has_text="w-known")).to_contain_text("2 / 4")
    unknown = rows.filter(has_text="w-unknown")
    expect(unknown).to_contain_text("? / 3")
    expect(unknown).not_to_contain_text("0 / 3")

    summary = page.get_by_text("Running now").locator("xpath=ancestor::div[contains(@class,'panel')][1]")
    expect(summary).to_contain_text("? / 7")
    expect(summary).to_contain_text("1 worker not reporting its load")

    unknown.click()
    expect(page.get_by_test_id("worker-detail")).to_contain_text("load not reported")
