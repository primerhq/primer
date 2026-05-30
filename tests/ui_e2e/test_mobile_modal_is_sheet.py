"""On a 375x812 viewport, a Modal renders as a .sheet-overlay (bottom
sheet), not as a .modal-overlay (centered dialog). Tap-outside and ESC
close it."""
from __future__ import annotations
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect  # noqa: E402


@pytest.mark.ui_e2e
def test_mobile_modal_renders_as_sheet(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{console_url}#/providers/llm")
    page.wait_for_load_state("domcontentloaded")
    page.locator(".fab").first.click()
    expect(page.locator(".sheet-overlay")).to_be_visible()
    expect(page.locator(".sheet-handle")).to_be_visible()
    expect(page.locator(".modal-overlay")).to_have_count(0)


@pytest.mark.ui_e2e
def test_mobile_modal_esc_closes(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{console_url}#/providers/llm")
    page.wait_for_load_state("domcontentloaded")
    page.locator(".fab").first.click()
    expect(page.locator(".sheet-overlay")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".sheet-overlay")).to_have_count(0)


@pytest.mark.ui_e2e
def test_mobile_modal_tap_backdrop_closes(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{console_url}#/providers/llm")
    page.wait_for_load_state("domcontentloaded")
    page.locator(".fab").first.click()
    expect(page.locator(".sheet-overlay")).to_be_visible()
    page.locator(".sheet-overlay").click(position={"x": 10, "y": 10})
    expect(page.locator(".sheet-overlay")).to_have_count(0)
