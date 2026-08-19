"""At 375x812 on every list route: no <table className='tbl'> should
render. The CardList branch supplies .card elements instead. Tapping
a card navigates to the corresponding detail route."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect  # noqa: E402


# Every list surface, named by the legacy route the facade translates.
# "/sessions" is absent: sessions are the shell's own rail and tabs, not
# a list page, and "/channels/associations" folded into the channels
# overlay's rules section.
LIST_ROUTES = [
    "/workspaces",
    "/workspaces/providers",
    "/workspaces/templates",
    "/agents",
    "/graphs",
    "/knowledge/collections",
    "/toolsets",
    "/providers/llm",
    "/providers/embedding",
    "/providers/cross_encoder",
    "/ssp",
    "/approvals",
    "/channels/providers",
    "/channels",
    "/channels/rules",
    "/harnesses",
]


from tests._support.smk import smk  # noqa: E402
from tests.ui_e2e._shell_helpers import open_legacy_route
pytestmark = smk("SMK-UI-01", status="partial")


@pytest.mark.ui_e2e
@pytest.mark.parametrize("route", LIST_ROUTES)
def test_mobile_no_table_on_list_route(
    page: Page, console_url: str, route: str
) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    open_legacy_route(page, console_url, route)
    page.wait_for_load_state("domcontentloaded")
    expect(page.locator("table.tbl")).to_have_count(0)


@pytest.mark.ui_e2e
def test_mobile_workspaces_tap_card_opens_detail(
    page: Page, console_url: str
) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    open_legacy_route(page, console_url, "workspaces")
    page.wait_for_load_state("domcontentloaded")
    # The card list is populated by an async fetch; give it time to
    # settle before counting so we don't read an empty pre-fetch DOM
    # (which would otherwise mask a real navigation regression as a
    # spurious skip).
    page.wait_for_load_state("networkidle", timeout=10_000)
    cards = page.locator(".card-interactive")
    try:
        cards.first.wait_for(state="visible", timeout=10_000)
    except Exception:
        pytest.skip("no workspaces seeded in this environment")
    cards.first.click()
    # Tapping a card navigates to the per-workspace detail route, i.e.
    # the hash gains a "/workspaces/<id>" segment (a non-empty id after
    # the trailing slash). ``to_have_url`` takes a string or regex, not
    # a callable, so match the detail-route shape with a pattern.
    expect(page).to_have_url(re.compile(r"#/workspaces/.+"))
