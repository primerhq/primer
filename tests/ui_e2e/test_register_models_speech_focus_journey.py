"""Journeys: where the focus goes inside the Register provider overlay when its Embedding, Cross Encoding, ASR and TTS pages change page (plan 02 Tasks 14 and 15; the page rules of #773).

These classes register a provider in two pages: the connection, then a list to choose from. Pressing Next removes Next (the footer of the second page is another footer), and
pressing Back removes Back; a focus on a removed element falls to ``<body>``, outside the dialog, on a desktop and on a phone. The page cannot fix that with an effect of its own:
its effects run BEFORE its new body is in the DOM (the overlay draws the body in a nested update that follows the page's commit), so the hand-off is a small component INSIDE the
body (``PC_PageFocus``), whose effect runs once the body has been drawn. The unit harness has no DOM and no focus, so the only place this can be pinned is here.

Nothing in the console opens the overlay yet (Task 20 switches the Providers page over), so the journey mounts it on the live console page itself, in a root of its own, exactly as a
page will: ``<PC_RegisterOverlay initialClass="embedding" ...>``. What a server would answer to Next comes from routed requests (``POST /v1/embedding_providers/_discover_models``,
``POST /v1/huggingface/_check_models``); ``GET /v1/<plural>/_types`` is the live server's own. The answer to Next is held until the test releases it (``_Hold``), never for a fixed
time: a pause is a flake on a slow runner.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk

pytestmark = smk("SMK-UI-06", status="partial")

LISTED = {"models": [{"name": "text-embedding-3-small"}, {"name": "gpt-4o"}]}
HUB_MODEL = "BAAI/bge-small-en-v1.5"
HUB_ANSWER = {"models": [{"name": HUB_MODEL, "ok": True, "status": "ok", "detail": "Can be pulled"}]}
ASR_ANSWER = {"ok": True, "models": ["whisper-1", "large-v3"]}
TTS_ANSWER = {"ok": True, "voices": ["alloy", "echo"]}

VIEWPORTS = [
    pytest.param({"width": 1366, "height": 768}, id="desktop"),
    pytest.param({"width": 390, "height": 844}, id="phone"),
]

# where the focus is: the data-testid of the focused element, whether it is inside the overlay's dialog, whether the page body has it
_FOCUS = """() => { const a = document.activeElement, d = document.querySelector('[data-testid="pc-register"]');
  return { testid: a && a.getAttribute('data-testid'), inDialog: !!(d && a && d.contains(a)), isBody: a === document.body, sheet: !!(d && d.classList.contains('sheet')) }; }"""

# the overlay in a root of its own on the live page, as the Providers page will mount it (create mode, one class)
_MOUNT = """(initialClass) => {
  const host = document.createElement('div');
  host.id = 'register-overlay-host';
  document.body.appendChild(host);
  window.__registerRoot = ReactDOM.createRoot(host);
  window.__registerRoot.render(React.createElement(window.PC_RegisterOverlay, {
    initialClass, allowedClasses: [initialClass], onClose() {}, onSaved() {},
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


def _json(route, body: dict, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def _mount(page: Page, viewport: dict, initial_class: str):
    """Mount the overlay at the viewport on one class and return the dialog once its fields are up."""
    page.set_viewport_size(viewport)
    page.wait_for_function("() => typeof window.PC_RegisterOverlay === 'function' && typeof ReactDOM !== 'undefined'", timeout=20_000)
    page.evaluate(_MOUNT, initial_class)
    dialog = page.get_by_test_id("pc-register")
    expect(dialog).to_be_visible(timeout=15_000)
    expect(dialog.get_by_test_id("pc-field:id")).to_be_visible(timeout=15_000)
    return dialog


def _open_openai(page: Page, viewport: dict, hold: _Hold | None = None):
    """The Embedding page with its models request routed (held first, when asked) and its connection filled in."""
    def discover(route) -> None:
        if hold is not None:
            hold.wait()
        _json(route, LISTED)

    page.route("**/v1/embedding_providers/_discover_models", discover)
    dialog = _mount(page, viewport, "embedding")
    dialog.get_by_test_id("pc-field:id").fill("emb-focus-journey")
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
def test_embedding_next_and_back_keep_the_focus_inside_the_dialog(page: Page, viewport: dict) -> None:
    """Next is pressed from the keyboard. The models page's heading has the focus once it is drawn (not ``<body>``, not the Next that is gone); Back, pressed from the keyboard,
    hands it to the connection page's first field, which still holds what was typed."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        dialog = _open_openai(page, viewport)
        dialog.get_by_test_id("pc-register-next").focus()
        page.keyboard.press("Enter")
        expect(dialog.get_by_test_id("pc-import-head")).to_be_visible(timeout=15_000)
        expect(dialog.get_by_test_id("pc-register-next")).to_have_count(0)
        page.evaluate(_SETTLED)
        landed = page.evaluate(_FOCUS)
        assert landed["sheet"] is (viewport["width"] < 640), f"the overlay is not a sheet exactly on a phone: {landed}"
        assert not landed["isBody"], f"after Next the focus fell to <body>: {landed}"
        assert landed["inDialog"] and landed["testid"] == "pc-import-head", f"the focus is not on the models page's heading inside the dialog: {landed}"
        expect(dialog.get_by_test_id("pc-import-head")).to_be_focused()
        # the page works from there: the embedding model is ticked, the chat model is not, and the button counts what is ticked
        expect(dialog.get_by_test_id("pc-import:text-embedding-3-small")).to_be_checked()
        expect(dialog.get_by_test_id("pc-import:gpt-4o")).not_to_be_checked()
        expect(dialog.get_by_test_id("pc-register-submit")).to_have_text("Register with 1 model")
        # Back
        dialog.get_by_test_id("pc-register-back").focus()
        page.keyboard.press("Enter")
        expect(dialog.get_by_test_id("pc-models-settings")).to_be_visible(timeout=15_000)
        expect(dialog.get_by_test_id("pc-register-back")).to_have_count(0)
        page.evaluate(_SETTLED)
        back = page.evaluate(_FOCUS)
        assert not back["isBody"], f"after Back the focus fell to <body>: {back}"
        assert back["inDialog"] and back["testid"] == "pc-field:id", f"the focus is not on the first field inside the dialog: {back}"
        expect(dialog.get_by_test_id("pc-field:id")).to_have_value("emb-focus-journey")
        assert errors == [], errors
    finally:
        _close(page, None)


@pytest.mark.ui_e2e
@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_the_hub_check_hands_the_focus_over_too(page: Page, viewport: dict) -> None:
    """A Hugging Face provider's Next asks the Hub, and the page of verdicts takes the focus the same way."""
    page.route("**/v1/huggingface/_check_models", lambda route: _json(route, HUB_ANSWER))
    try:
        dialog = _mount(page, viewport, "embedding")
        dialog.get_by_test_id("pc-field:type").select_option("huggingface")
        dialog.get_by_test_id("pc-field:id").fill("hf-focus-journey")
        dialog.get_by_test_id("pc-models:0:name").fill(HUB_MODEL)
        expect(dialog.get_by_test_id("pc-register-next")).to_be_enabled()
        dialog.get_by_test_id("pc-register-next").focus()
        page.keyboard.press("Enter")
        expect(dialog.get_by_test_id("pc-import-head")).to_be_visible(timeout=15_000)
        page.evaluate(_SETTLED)
        landed = page.evaluate(_FOCUS)
        assert not landed["isBody"], f"after Next the focus fell to <body>: {landed}"
        assert landed["inDialog"] and landed["testid"] == "pc-import-head", f"the focus is not on the page's heading inside the dialog: {landed}"
        expect(dialog.get_by_test_id(f"pc-import-status:{HUB_MODEL}")).to_have_text("Can be pulled")
        expect(dialog.get_by_test_id("pc-register-submit")).to_have_text("Register with 1 model")
    finally:
        _close(page, None)


@pytest.mark.ui_e2e
@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_next_leaves_a_focus_the_operator_moved(page: Page, viewport: dict) -> None:
    """The answer takes a moment. An operator who has put the focus somewhere that stays (here: the type selector, which belongs to the shell) keeps it when the list arrives."""
    hold = _Hold(page)
    try:
        dialog = _open_openai(page, viewport, hold)
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
def test_a_refused_register_keeps_the_focus_on_the_button_that_was_pressed(page: Page, viewport: dict) -> None:
    """The footer's right button is ONE button through the request, and it is ``aria-busy`` (not ``disabled``) while it is out, so a focus on it is not lost when the server
    refuses the provider and the overlay stays open with the reason."""
    page.route("**/v1/embedding_providers", lambda route: _json(
        route, {"type": "/errors/conflict", "title": "Conflict", "status": 409, "detail": "EmbeddingProvider with id 'emb-focus-journey' already exists",
                "instance": "/v1/embedding_providers"}, 409) if route.request.method == "POST" else route.continue_())
    try:
        dialog = _open_openai(page, viewport)
        dialog.get_by_test_id("pc-register-next").focus()
        page.keyboard.press("Enter")
        expect(dialog.get_by_test_id("pc-import-head")).to_be_visible(timeout=15_000)
        submit = dialog.get_by_test_id("pc-register-submit")
        submit.focus()
        page.keyboard.press("Enter")
        expect(dialog.get_by_test_id("pc-register-error")).to_contain_text("already exists", timeout=15_000)
        page.evaluate(_SETTLED)
        landed = page.evaluate(_FOCUS)
        assert not landed["isBody"], f"the focus fell to <body> when the provider was refused: {landed}"
        assert landed["inDialog"] and landed["testid"] == "pc-register-submit", f"the focus is not on the button that was pressed: {landed}"
    finally:
        _close(page, None)


@pytest.mark.ui_e2e
@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_asr_next_and_back_keep_the_focus_inside_the_dialog(page: Page, viewport: dict) -> None:
    """The ASR page needs only a URL and a key to go on. Next (from the keyboard) lands on the heading of the page that picks the model; the radio list works from there; Back hands the
    focus to the first field, which still holds what was typed, and the pick is kept for the same connection."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.route("**/v1/stt_providers/_test", lambda route: _json(route, ASR_ANSWER))
    try:
        dialog = _mount(page, viewport, "stt")
        dialog.get_by_test_id("pc-field:id").fill("asr-focus-journey")
        dialog.get_by_test_id("pc-field:url").fill("http://asr.invalid:8006/v1")
        expect(dialog.get_by_test_id("pc-register-next")).to_be_enabled()
        dialog.get_by_test_id("pc-register-next").focus()
        page.keyboard.press("Enter")
        expect(dialog.get_by_test_id("pc-import-head")).to_be_visible(timeout=15_000)
        expect(dialog.get_by_test_id("pc-register-next")).to_have_count(0)
        page.evaluate(_SETTLED)
        landed = page.evaluate(_FOCUS)
        assert landed["sheet"] is (viewport["width"] < 640), f"the overlay is not a sheet exactly on a phone: {landed}"
        assert not landed["isBody"], f"after Next the focus fell to <body>: {landed}"
        assert landed["inDialog"] and landed["testid"] == "pc-import-head", f"the focus is not on the choice page's heading inside the dialog: {landed}"
        # nothing is picked, so Register is off; picking one turns it on
        expect(dialog.get_by_test_id("pc-pick:whisper-1")).not_to_be_checked()
        expect(dialog.get_by_test_id("pc-register-submit")).to_have_attribute("disabled", "")
        dialog.get_by_test_id("pc-pick:large-v3").check()
        expect(dialog.get_by_test_id("pc-register-submit")).to_be_enabled()
        # Back
        dialog.get_by_test_id("pc-register-back").focus()
        page.keyboard.press("Enter")
        expect(dialog.get_by_test_id("pc-speech-settings")).to_be_visible(timeout=15_000)
        expect(dialog.get_by_test_id("pc-register-back")).to_have_count(0)
        page.evaluate(_SETTLED)
        back = page.evaluate(_FOCUS)
        assert not back["isBody"], f"after Back the focus fell to <body>: {back}"
        assert back["inDialog"] and back["testid"] == "pc-field:id", f"the focus is not on the first field inside the dialog: {back}"
        expect(dialog.get_by_test_id("pc-field:id")).to_have_value("asr-focus-journey")
        # the same connection again: no second test, and the pick is still there
        dialog.get_by_test_id("pc-register-next").focus()
        page.keyboard.press("Enter")
        expect(dialog.get_by_test_id("pc-import-head")).to_be_visible(timeout=15_000)
        expect(dialog.get_by_test_id("pc-pick:large-v3")).to_be_checked()
        assert errors == [], errors
    finally:
        _close(page, None)


@pytest.mark.ui_e2e
@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_tts_next_hands_the_focus_to_the_voice_page(page: Page, viewport: dict) -> None:
    """The TTS page also asks for the default model; its Next lands on the heading of the page that picks the voice."""
    page.route("**/v1/tts_providers/_test", lambda route: _json(route, TTS_ANSWER))
    try:
        dialog = _mount(page, viewport, "tts")
        dialog.get_by_test_id("pc-field:id").fill("tts-focus-journey")
        dialog.get_by_test_id("pc-field:url").fill("http://tts.invalid:8004/v1")
        dialog.get_by_test_id("pc-field:default_model").fill("kokoro")
        expect(dialog.get_by_test_id("pc-register-next")).to_be_enabled()
        dialog.get_by_test_id("pc-register-next").focus()
        page.keyboard.press("Enter")
        expect(dialog.get_by_test_id("pc-import-head")).to_be_visible(timeout=15_000)
        page.evaluate(_SETTLED)
        landed = page.evaluate(_FOCUS)
        assert not landed["isBody"], f"after Next the focus fell to <body>: {landed}"
        assert landed["inDialog"] and landed["testid"] == "pc-import-head", f"the focus is not on the voice page's heading inside the dialog: {landed}"
        expect(dialog.get_by_test_id("pc-import-head")).to_contain_text("Voices found at tts-focus-journey")
        expect(dialog.get_by_test_id("pc-pick:alloy")).to_be_visible()
    finally:
        _close(page, None)
