"""Journeys: where the focus goes inside the Register provider overlay when a Try again works (re-review of #773).

The overlay (``window.PC_RegisterOverlay``, plan 02 Task 11) owns ONE dialog, and a page publishes its body and footer into slots in it. The slots draw the body in a
nested update that runs AFTER the page's own effects. The form's hand-off after a Try again used to be such an effect: it looked at the focus, saw the OLD body (the Try
again button and the alert, still in the DOM and still focused), concluded that the operator had put the focus somewhere, and did nothing; one task later the button was
gone and the focus fell to ``<body>``, outside the dialog, on a desktop and on a phone. The hand-off is now a component inside the body, so it runs when the new body has
been drawn. The unit harness has no DOM and no focus, so the only place this can be pinned is here.

Nothing in the console opens the overlay yet (Task 20 switches the Providers page over), so the journey mounts it on the live console page itself, in a root of its own,
exactly as a page will: ``<PC_RegisterOverlay initialClass="web_search" ...>``. The first ``GET /v1/web_search_providers/_types`` is made to fail once; Try again is pressed
from the keyboard.

The second request is held until the test releases it (``_Hold``), never for a fixed time: a pause is a flake on a slow runner.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk

pytestmark = smk("SMK-UI-06", status="partial")

TYPES_500 = {"type": "/errors/internal", "title": "Internal Server Error", "status": 500, "detail": "types unavailable", "instance": "/v1/web_search_providers/_types"}

VIEWPORTS = [
    pytest.param({"width": 1366, "height": 768}, id="desktop"),
    pytest.param({"width": 390, "height": 844}, id="phone"),
]

# where the focus is: the data-testid of the focused element, whether it is inside the overlay's dialog, whether the page body has it
_FOCUS = """() => { const a = document.activeElement, d = document.querySelector('[data-testid="pc-register"]');
  return { testid: a && a.getAttribute('data-testid'), inDialog: !!(d && a && d.contains(a)), isBody: a === document.body, sheet: !!(d && d.classList.contains('sheet')) }; }"""

# the overlay in a root of its own on the live page, as the Providers page will mount it (create mode, Web Search only)
_MOUNT = """() => {
  const host = document.createElement('div');
  host.id = 'register-overlay-host';
  document.body.appendChild(host);
  window.__registerRoot = ReactDOM.createRoot(host);
  window.__registerRoot.render(React.createElement(window.PC_RegisterOverlay, {
    initialClass: 'web_search', allowedClasses: ['web_search'], onClose() {}, onSaved() {},
  }));
}"""


class _Hold:
    """Requests held until the test releases them. The handler waits in small steps; the cap only keeps a broken test from hanging."""

    def __init__(self, page: Page, cap_ms: int = 30_000) -> None:
        self.page, self.cap_ms, self.arrived, self.released = page, cap_ms, 0, False

    def wait(self) -> None:
        self.arrived += 1
        waited = 0
        while not self.released and waited < self.cap_ms:
            self.page.wait_for_timeout(25)
            waited += 25

    def release(self) -> None:
        self.released = True


def _open_with_a_failing_types_request(page: Page, viewport: dict, hold: _Hold, calls: dict):
    """Mount the overlay at the viewport with the first ``_types`` request failing and the second held; returns the dialog and the Try again button once the alert is up."""
    def flaky(route) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(status=500, content_type="application/problem+json", body=json.dumps(TYPES_500))
        else:
            hold.wait()
            route.continue_()

    page.route("**/v1/web_search_providers/_types", flaky)
    page.set_viewport_size(viewport)
    page.wait_for_function("() => typeof window.PC_RegisterOverlay === 'function' && typeof ReactDOM !== 'undefined'", timeout=20_000)
    page.evaluate(_MOUNT)
    dialog = page.get_by_test_id("pc-register")
    expect(dialog).to_be_visible(timeout=15_000)
    expect(dialog.get_by_test_id("provider-form-types-error")).to_be_visible(timeout=15_000)
    expect(dialog.get_by_test_id("provider-form-types-error")).to_have_attribute("role", "alert")
    retry = dialog.get_by_test_id("provider-form-types-retry")
    expect(retry).to_be_visible()
    return dialog, retry


@pytest.mark.ui_e2e
@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_a_try_again_that_works_puts_the_focus_on_the_first_field_inside_the_dialog(page: Page, viewport: dict) -> None:
    """Try again is pressed from the keyboard; while the retry is out the button says so and keeps the focus; when the answer arrives the button is gone, and the focus is on
    the form's first field (the Name, ``pc-field:id``) INSIDE the dialog: not on ``<body>``, not left on a button that no longer exists."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    hold, calls = _Hold(page), {"n": 0}
    try:
        dialog, retry = _open_with_a_failing_types_request(page, viewport, hold, calls)
        retry.focus()
        page.keyboard.press("Enter")
        expect(retry).to_have_attribute("aria-busy", "true")
        expect(retry).to_contain_text("Trying again")
        busy = page.evaluate(_FOCUS)
        assert busy["testid"] == "provider-form-types-retry" and busy["inDialog"], f"the focus left Try again while the retry was out: {busy}"
        hold.release()
        # the kind's own fields are the sign that the answer has been drawn (the Name and the Type are there even while the request fails)
        expect(dialog.get_by_test_id("pc-field:api_key")).to_be_visible(timeout=15_000)
        expect(dialog.get_by_test_id("provider-form-types-error")).to_have_count(0)
        expect(retry).to_have_count(0)
        landed = page.evaluate(_FOCUS)
        assert landed["sheet"] is (viewport["width"] < 640), f"the overlay is not a sheet exactly on a phone: {landed}"
        assert not landed["isBody"], f"after a Try again that worked the focus fell to <body>: {landed}"
        assert landed["inDialog"] and landed["testid"] == "pc-field:id", f"the focus is not on the first field inside the dialog: {landed}"
        expect(dialog.get_by_test_id("pc-field:id")).to_be_focused()
        assert calls["n"] == 2, f"{calls['n']} _types requests were made"
        assert errors == [], errors
    finally:
        hold.release()
        page.evaluate("() => { if (window.__registerRoot) window.__registerRoot.unmount(); }")
        page.unroute_all(behavior="ignoreErrors")


@pytest.mark.ui_e2e
@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_a_try_again_that_works_leaves_a_focus_the_operator_moved(page: Page, viewport: dict) -> None:
    """The retry takes a moment. An operator who has put the focus somewhere else while it was out (here: on Cancel) keeps it when the answer arrives."""
    hold, calls = _Hold(page), {"n": 0}
    try:
        dialog, retry = _open_with_a_failing_types_request(page, viewport, hold, calls)
        retry.focus()
        page.keyboard.press("Enter")
        expect(retry).to_have_attribute("aria-busy", "true")
        dialog.get_by_test_id("provider-form-cancel").focus()
        hold.release()
        expect(dialog.get_by_test_id("pc-field:api_key")).to_be_visible(timeout=15_000)
        expect(retry).to_have_count(0)
        landed = page.evaluate(_FOCUS)
        assert landed["testid"] == "provider-form-cancel" and landed["inDialog"], f"the hand-off took the focus from where the operator put it: {landed}"
    finally:
        hold.release()
        page.evaluate("() => { if (window.__registerRoot) window.__registerRoot.unmount(); }")
        page.unroute_all(behavior="ignoreErrors")
