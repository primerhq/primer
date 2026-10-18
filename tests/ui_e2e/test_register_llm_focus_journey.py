"""Journeys: where the focus goes inside the Register provider overlay when its LLM page changes page (plan 02 Task 12; the page rules of #773).

The LLM page registers a provider in two pages. Pressing Next removes Next (the footer of the models page is another footer), and pressing Back removes Back; a focus on a
removed element falls to ``<body>``, outside the dialog, on a desktop and on a phone. The page cannot fix that with an effect of its own: its effects run BEFORE its new body
is in the DOM (the overlay draws the body in a nested update that follows the page's commit), so the hand-off is a small component INSIDE the body (``PC_LlmFocus``), whose
effect runs once the body has been drawn. The unit harness has no DOM and no focus, so the only place this can be pinned is here.

Nothing in the console opens the overlay yet (Task 20 switches the Providers page over), so the journey mounts it on the live console page itself, in a root of its own, exactly
as a page will: ``<PC_RegisterOverlay initialClass="llm" ...>``. The model list comes from a routed ``POST /v1/llm_providers/_discover_models``; ``GET /v1/llm_providers/_types``
is the live server's own. The answer to Next is held until the test releases it (``_Hold``), never for a fixed time: a pause is a flake on a slow runner.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk

pytestmark = smk("SMK-UI-06", status="partial")

MODELS = {"models": [{"name": "model-one", "context_length": 4096}, {"name": "model-two"}]}

VIEWPORTS = [
    pytest.param({"width": 1366, "height": 768}, id="desktop"),
    pytest.param({"width": 390, "height": 844}, id="phone"),
]

# where the focus is: the data-testid of the focused element, whether it is inside the overlay's dialog, whether the page body has it
_FOCUS = """() => { const a = document.activeElement, d = document.querySelector('[data-testid="pc-register"]');
  return { testid: a && a.getAttribute('data-testid'), inDialog: !!(d && a && d.contains(a)), isBody: a === document.body, sheet: !!(d && d.classList.contains('sheet')) }; }"""

# the overlay in a root of its own on the live page, as the Providers page will mount it (create mode, LLM only)
_MOUNT = """() => {
  const host = document.createElement('div');
  host.id = 'register-overlay-host';
  document.body.appendChild(host);
  window.__registerRoot = ReactDOM.createRoot(host);
  window.__registerRoot.render(React.createElement(window.PC_RegisterOverlay, {
    initialClass: 'llm', allowedClasses: ['llm'], onClose() {}, onSaved() {},
  }));
}"""


# two animation frames and a task: React flushes the passive effects of a render before the next frame is drawn, so once this resolves a hand-off that was going to happen
# has happened. It is a sync point with the browser, not a pause.
_SETTLED = """() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, 0))))"""


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


def _open_filled(page: Page, viewport: dict, hold: _Hold | None = None):
    """Mount the overlay at the viewport with the models request routed (held first, when asked), fill the connection page, and return the dialog."""
    def discover(route) -> None:
        if hold is not None:
            hold.wait()
        route.fulfill(status=200, content_type="application/json", body=json.dumps(MODELS))

    page.route("**/v1/llm_providers/_discover_models", discover)
    page.set_viewport_size(viewport)
    page.wait_for_function("() => typeof window.PC_RegisterOverlay === 'function' && typeof ReactDOM !== 'undefined'", timeout=20_000)
    page.evaluate(_MOUNT)
    dialog = page.get_by_test_id("pc-register")
    expect(dialog).to_be_visible(timeout=15_000)
    expect(dialog.get_by_test_id("pc-field:url")).to_be_visible(timeout=15_000)
    dialog.get_by_test_id("pc-field:id").fill("lm-focus-journey")
    dialog.get_by_test_id("pc-field:url").fill("http://models.invalid/v1")
    expect(dialog.get_by_test_id("pc-register-next")).to_be_enabled()
    return dialog


def _close(page: Page, hold: _Hold | None) -> None:
    if hold is not None:
        hold.release()
    page.evaluate("() => { if (window.__registerRoot) window.__registerRoot.unmount(); }")
    page.unroute_all(behavior="ignoreErrors")


@pytest.mark.ui_e2e
@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_next_and_back_keep_the_focus_inside_the_dialog(page: Page, viewport: dict) -> None:
    """Next is pressed from the keyboard. The models page's heading has the focus once it is drawn (not ``<body>``, not the Next that is gone); Back, pressed from the
    keyboard, hands it to the connection page's first field."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        dialog = _open_filled(page, viewport)
        next_button = dialog.get_by_test_id("pc-register-next")
        next_button.focus()
        page.keyboard.press("Enter")
        expect(dialog.get_by_test_id("pc-import-head")).to_be_visible(timeout=15_000)
        expect(dialog.get_by_test_id("pc-register-next")).to_have_count(0)
        page.evaluate(_SETTLED)
        landed = page.evaluate(_FOCUS)
        assert landed["sheet"] is (viewport["width"] < 640), f"the overlay is not a sheet exactly on a phone: {landed}"
        assert not landed["isBody"], f"after Next the focus fell to <body>: {landed}"
        assert landed["inDialog"] and landed["testid"] == "pc-import-head", f"the focus is not on the models page's heading inside the dialog: {landed}"
        expect(dialog.get_by_test_id("pc-import-head")).to_be_focused()
        # the page works from there: tick a model, and the button counts it
        dialog.get_by_test_id("pc-import:model-one").check()
        expect(dialog.get_by_test_id("pc-register-submit")).to_have_text("Import 1 model")
        # Back
        dialog.get_by_test_id("pc-register-back").focus()
        page.keyboard.press("Enter")
        expect(dialog.get_by_test_id("pc-llm-settings")).to_be_visible(timeout=15_000)
        expect(dialog.get_by_test_id("pc-register-back")).to_have_count(0)
        page.evaluate(_SETTLED)
        back = page.evaluate(_FOCUS)
        assert not back["isBody"], f"after Back the focus fell to <body>: {back}"
        assert back["inDialog"] and back["testid"] == "pc-field:id", f"the focus is not on the first field inside the dialog: {back}"
        expect(dialog.get_by_test_id("pc-field:id")).to_have_value("lm-focus-journey")
        assert errors == [], errors
    finally:
        _close(page, None)


@pytest.mark.ui_e2e
@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_next_leaves_a_focus_the_operator_moved(page: Page, viewport: dict) -> None:
    """The answer takes a moment. An operator who has put the focus somewhere that stays (here: the type selector, which belongs to the shell) keeps it when the list arrives."""
    hold = _Hold(page)
    try:
        dialog = _open_filled(page, viewport, hold)
        next_button = dialog.get_by_test_id("pc-register-next")
        next_button.focus()
        page.keyboard.press("Enter")
        expect(next_button).to_have_attribute("aria-busy", "true")
        dialog.get_by_test_id("pc-register-type").focus()
        hold.release()
        expect(dialog.get_by_test_id("pc-import-head")).to_be_visible(timeout=15_000)
        page.evaluate(_SETTLED)
        landed = page.evaluate(_FOCUS)
        assert landed["testid"] == "pc-register-type" and landed["inDialog"], f"the hand-off took the focus from where the operator put it: {landed}"
    finally:
        _close(page, hold)


@pytest.mark.ui_e2e
@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_the_button_that_started_the_import_keeps_its_focus_when_the_import_ends_in_failures(page: Page, viewport: dict) -> None:
    """The footer's right button is ONE button from "Import 1 model" to "Retry failed", so a focus on it is not lost when a profile is refused and the overlay stays open."""
    def profiles(route) -> None:
        route.fulfill(status=500, content_type="application/problem+json", body=json.dumps(
            {"type": "/errors/internal", "title": "Internal Server Error", "status": 500, "detail": "the database went away", "instance": "/v1/model_profiles"}))

    def provider(route) -> None:
        route.fulfill(status=201, content_type="application/json", body=json.dumps({"id": "lm-focus-journey"}))

    page.route("**/v1/model_profiles", profiles)
    page.route("**/v1/llm_providers", lambda route: provider(route) if route.request.method == "POST" else route.continue_())
    try:
        dialog = _open_filled(page, viewport)
        dialog.get_by_test_id("pc-register-next").focus()
        page.keyboard.press("Enter")
        expect(dialog.get_by_test_id("pc-import-head")).to_be_visible(timeout=15_000)
        dialog.get_by_test_id("pc-import:model-two").check()
        dialog.get_by_test_id("pc-import-context:model-two").fill("2048")
        submit = dialog.get_by_test_id("pc-register-submit")
        submit.focus()
        page.keyboard.press("Enter")
        retry = dialog.get_by_test_id("pc-register-retry-failed")
        expect(retry).to_be_visible(timeout=15_000)
        expect(dialog.get_by_test_id("pc-import-status:model-two")).to_contain_text("failed")
        page.evaluate(_SETTLED)
        landed = page.evaluate(_FOCUS)
        assert not landed["isBody"], f"the focus fell to <body> when the import ended in a failure: {landed}"
        assert landed["inDialog"] and landed["testid"] == "pc-register-retry-failed", f"the focus is not on the button that now says Retry failed: {landed}"
    finally:
        _close(page, None)
