"""On a 375x812 viewport, a Modal renders as a .sheet-overlay (bottom
sheet), not as a .modal-overlay (centered dialog). Tap-outside and ESC
close it.

RETARGET (US-014): this used to open the Agents surface and tap its FAB,
but at mobile widths NV_MobileShell now intercepts the "agents" overlay
before it ever mounts (it lands on the More tab's read-only fact-sheet
flow instead - no FAB, no create, "Edit on desktop") - see
nv-mobile-shell.jsx's con.overlay effect. The thing actually under test is
shared.jsx's Modal component, which decides .sheet-overlay vs
.modal-overlay from useViewport().isMobile (pure viewport width, the same
hook regardless of which shell mounted it) - any reachable Modal+trigger
proves the same behavior. Workspace providers ("providers:workspace") is
NOT one of NV_MobileShell's intercepted nav ids (provider-catalog.jsx has
no NV_PLAT_PAGES entry - see nv-platform.jsx's own comment on why), so it
still falls through to the classic overlay and its real "New provider"
modal, same flow test_workspace_providers_journey.py drives at desktop
width."""
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


def _open_create_sheet(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    open_legacy_route(page, console_url, "workspaces/providers")
    page.wait_for_load_state("domcontentloaded")
    new_btn = page.get_by_role(
        "button", name="New workspace provider",
    ).or_(
        page.get_by_role("button", name="New provider")
    ).first
    expect(new_btn).to_be_visible(timeout=10_000)
    new_btn.click()


@pytest.mark.ui_e2e
@_MOBILE_OVERLAY_FLAKY
def test_mobile_modal_renders_as_sheet(page: Page, console_url: str) -> None:
    _open_create_sheet(page, console_url)
    expect(page.locator(".sheet-overlay")).to_be_visible()
    expect(page.locator(".sheet-handle")).to_be_visible()
    expect(page.locator(".modal-overlay")).to_have_count(0)


@pytest.mark.ui_e2e
@_MOBILE_OVERLAY_FLAKY
def test_mobile_modal_esc_closes(page: Page, console_url: str) -> None:
    _open_create_sheet(page, console_url)
    expect(page.locator(".sheet-overlay")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".sheet-overlay")).to_have_count(0)


@pytest.mark.ui_e2e
@_MOBILE_OVERLAY_FLAKY
def test_mobile_modal_tap_backdrop_closes(page: Page, console_url: str) -> None:
    _open_create_sheet(page, console_url)
    expect(page.locator(".sheet-overlay")).to_be_visible()
    page.locator(".sheet-overlay").click(position={"x": 10, "y": 10})
    expect(page.locator(".sheet-overlay")).to_have_count(0)
