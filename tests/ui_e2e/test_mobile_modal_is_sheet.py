"""On a 375x812 viewport, a Modal renders as a .sheet-overlay (bottom
sheet), not as a .modal-overlay (centered dialog). Tap-outside and ESC
close it.

Driven from the AGENTS surface. These opened the provider catalog and
tapped its FAB, but the catalog creates through an inline form rather
than a modal and so has no FAB to tap: the per-class pages that did are
what it replaced. Agents still creates through a modal, which is the
thing under test here -- the surface is only the way in."""
from __future__ import annotations
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect  # noqa: E402


from tests._support.smk import smk  # noqa: E402
from tests.ui_e2e._shell_helpers import open_legacy_route
pytestmark = smk("SMK-UI-01", status="partial")

# US-011b: the pre-existing ui_e2e overlay-mount race (orig handoff
# known-issue #1) is AMPLIFIED at mobile viewports (<400px) - three CI
# runs produced three DISJOINT failing sets among this file's three
# tests, with no shared per-route cause (.omc/progress.txt). Root-
# causing the race is a separate, longer-term item; this retries only
# these mobile (375px, under the 400px line) params, never a desktop
# test, so a real regression still fails outright after the reruns.
_MOBILE_OVERLAY_FLAKY = pytest.mark.flaky(reruns=2, reruns_delay=1)


@pytest.mark.ui_e2e
@_MOBILE_OVERLAY_FLAKY
def test_mobile_modal_renders_as_sheet(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    open_legacy_route(page, console_url, "agents")
    page.wait_for_load_state("domcontentloaded")
    page.locator(".fab").first.click()
    expect(page.locator(".sheet-overlay")).to_be_visible()
    expect(page.locator(".sheet-handle")).to_be_visible()
    expect(page.locator(".modal-overlay")).to_have_count(0)


@pytest.mark.ui_e2e
@_MOBILE_OVERLAY_FLAKY
def test_mobile_modal_esc_closes(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    open_legacy_route(page, console_url, "agents")
    page.wait_for_load_state("domcontentloaded")
    page.locator(".fab").first.click()
    expect(page.locator(".sheet-overlay")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".sheet-overlay")).to_have_count(0)


@pytest.mark.ui_e2e
@_MOBILE_OVERLAY_FLAKY
def test_mobile_modal_tap_backdrop_closes(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    open_legacy_route(page, console_url, "agents")
    page.wait_for_load_state("domcontentloaded")
    page.locator(".fab").first.click()
    expect(page.locator(".sheet-overlay")).to_be_visible()
    page.locator(".sheet-overlay").click(position={"x": 10, "y": 10})
    expect(page.locator(".sheet-overlay")).to_have_count(0)
