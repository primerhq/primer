"""At 375x812 on every list route: no <table className='tbl'> should
render. The CardList branch supplies .card elements instead. Tapping
a card navigates to the corresponding detail route."""

from __future__ import annotations

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect  # noqa: E402


LIST_ROUTES = [
    "/sessions",
    "/workspaces",
    "/workspaces/providers",
    "/workspaces/templates",
    "/agents",
    "/graphs",
    "/knowledge/collections",
    "/knowledge/documents",
    "/toolsets",
    "/providers/llm",
    "/providers/embedding",
    "/providers/cross_encoder",
    "/ssp",
    "/approvals",
    "/channels/providers",
    "/channels/channels",
    "/channels/associations",
    "/harnesses",
]


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-01", status="partial")


@pytest.mark.ui_e2e
@pytest.mark.parametrize("route", LIST_ROUTES)
def test_mobile_no_table_on_list_route(
    page: Page, console_url: str, route: str
) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{console_url}#{route}")
    page.wait_for_load_state("domcontentloaded")
    expect(page.locator("table.tbl")).to_have_count(0)


@pytest.mark.ui_e2e
def test_mobile_workspaces_tap_card_opens_detail(
    page: Page, console_url: str
) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{console_url}#/workspaces")
    page.wait_for_load_state("domcontentloaded")
    cards = page.locator(".card-interactive")
    if cards.count() == 0:
        pytest.skip("no workspaces seeded in this environment")
    cards.first.click()
    expect(page).to_have_url(lambda u: "/workspaces/" in u and not u.endswith("/workspaces"))
