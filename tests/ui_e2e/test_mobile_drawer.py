"""At 375x812: hamburger is the only thing in the topbar's left half,
sidebar is hidden, tap hamburger → drawer slides in, ESC + backdrop
close it, route change closes it."""
from __future__ import annotations
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect  # noqa: E402


@pytest.mark.ui_e2e
def test_mobile_hamburger_visible_sidebar_hidden(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{console_url}#/")
    page.wait_for_load_state("domcontentloaded")
    expect(page.locator(".hamburger")).to_be_visible()
    expect(page.locator(".sidebar:not(.drawer .sidebar)")).to_be_hidden()


@pytest.mark.ui_e2e
def test_mobile_drawer_opens_on_hamburger(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{console_url}#/")
    page.wait_for_load_state("domcontentloaded")
    page.locator(".hamburger").click()
    expect(page.locator(".drawer.open")).to_be_visible()


@pytest.mark.ui_e2e
def test_mobile_drawer_closes_on_escape(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{console_url}#/")
    page.wait_for_load_state("domcontentloaded")
    page.locator(".hamburger").click()
    expect(page.locator(".drawer.open")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".drawer.open")).to_have_count(0)


@pytest.mark.ui_e2e
def test_mobile_drawer_closes_on_backdrop_tap(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{console_url}#/")
    page.wait_for_load_state("domcontentloaded")
    page.locator(".hamburger").click()
    expect(page.locator(".drawer.open")).to_be_visible()
    page.locator(".drawer-overlay").click(position={"x": 360, "y": 400})
    expect(page.locator(".drawer.open")).to_have_count(0)


@pytest.mark.ui_e2e
def test_mobile_drawer_closes_on_route_change(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{console_url}#/")
    page.wait_for_load_state("domcontentloaded")
    page.locator(".hamburger").click()
    expect(page.locator(".drawer.open")).to_be_visible()
    page.locator(".drawer .nav-item", has_text="Sessions").first.click()
    expect(page.locator(".drawer.open")).to_have_count(0)
