"""Journey: the System dashboard's health cards say what the server says (console review C-036, the console part).

The scheduler card shows ``scheduler.detail`` from ``GET /v1/health`` under its word (the default install reports "in-memory scheduler (single process assumed)"; a scheduler with nothing
to add reads "healthy"), and the sessions-active card shows the ``total`` of ``GET /v1/sessions?session_state=running``: the turns in flight right now, not every session marked running.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-06", status="partial")


@pytest.mark.ui_e2e
def test_the_dashboard_cards_show_the_schedulers_detail_and_the_turns_in_flight(page: Page, base_url: str, console_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        scheduler = c.get("/v1/health").json()["scheduler"]
        running = c.get("/v1/sessions", params={"session_state": "running", "limit": 1}).json()["total"]
    assert scheduler["alive"] and not scheduler["degraded"], "this journey reads a healthy install"
    open_view(page, console_url, "primer", "system:dashboard")
    cards = page.get_by_test_id("nv-sys-health")
    expect(cards).to_be_visible(timeout=20_000)

    scheduler_card = cards.locator(".nv-health-card").filter(has=page.locator(".nv-health-k", has_text="scheduler"))
    expect(scheduler_card.locator(".nv-health-v")).to_have_text("alive", timeout=15_000)
    expect(scheduler_card.locator(".nv-health-sub")).to_have_text(scheduler["detail"] or "healthy")

    sessions_card = cards.locator(".nv-health-card").filter(has=page.locator(".nv-health-k", has_text="sessions active"))
    expect(sessions_card.locator(".nv-health-v")).to_have_text(str(running), timeout=15_000)
    expect(sessions_card.locator(".nv-health-sub")).to_have_text("running now")
