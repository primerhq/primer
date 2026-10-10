"""Journey: a focus that falls to ``<body>`` is still trapped (board task 01a124ad, T1, measured in the review of #717).

``useFocusTrap`` (``ui/foundation/focus-trap.js``) listened for Tab on the dialog node, so a key whose target is ``<body>`` never reached it. In the Create session overlay, Enter on a focused "Create session" disables
the button while its request is out; Chromium then moves the focus of a disabled control to ``<body>`` (within about 50 ms), and the next Tab went to the first control of the page behind the scrim
(``nv-go-studio``). The same happens when the focused control is removed with no hand-off, which is why the fix is in the trap and not only in this one button (the systemic busy-disabled controls are a ticket of
their own). The POST is held, so the test does not race the server.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from tests.ui_e2e._shell_helpers import open_shell

_WHERE = """() => {
  const d = document.querySelector('[role=dialog]'), a = document.activeElement;
  return { inside: !!(d && a && d.contains(a)), body: a === document.body, testid: (a && a.getAttribute && a.getAttribute('data-testid')) || null };
}"""


def _a_workspace_id(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        return client.get("/v1/workspaces").json()["items"][0]["id"]


@pytest.mark.ui_e2e
def test_tab_after_the_focused_submit_went_busy_stays_in_the_overlay(page: Page, base_url: str, console_url: str) -> None:
    held: list = []
    page.route("**/v1/workspaces/*/sessions", lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
    try:
        open_shell(page, console_url, _a_workspace_id(base_url))
        page.get_by_test_id("nv-rail-create-session").click()
        expect(page.get_by_role("dialog")).to_be_visible(timeout=10_000)
        create = page.get_by_test_id("nv-ns-create")
        expect(create).to_be_enabled(timeout=10_000)
        create.focus()
        page.keyboard.press("Enter")
        page.wait_for_function("() => document.querySelector('[data-testid=nv-ns-create]').disabled === true", timeout=5_000)
        try:
            page.wait_for_function("() => document.activeElement === document.body", timeout=3_000)     # Chromium hands the focus of a disabled control to <body>
        except PlaywrightTimeout:
            pass                                                                                       # a build that keeps it on the button is fine too: the Tabs below must stay inside either way
        for key in ("Tab", "Tab", "Shift+Tab", "Shift+Tab"):
            page.keyboard.press(key)
            state = page.evaluate(_WHERE)
            assert state["inside"], f"{key} with the submit busy left the overlay for the page behind it: {state}"
    finally:
        for route in held:
            route.fulfill(status=503, content_type="application/problem+json", body='{"type":"about:blank","title":"held","status":503,"detail":"released by the test"}')
        page.wait_for_timeout(200)
        page.unroute_all(behavior="ignoreErrors")
