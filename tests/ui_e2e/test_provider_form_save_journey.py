"""Journeys: the provider form never offers Save before the kind's fields have loaded, a Save the server refuses is said in the form, and the focus stays in the modal throughout (board task 01a12350-792d).

Found on main by the lead: ``test_u0047_provider_list_reflects_new_row_after_modal_create`` failed on main 919f3f28 and on #700 and passed on neighbouring commits. The form fetched ``GET /<plural>/_types`` a
second time (under another cache key than the Register menu), and until that answer arrived it offered an enabled Save with no Limits and no API key box; the POST had no ``limits`` and answered 422, which the
catalog's ``save()`` let go as an unhandled rejection: the modal stayed open and said nothing.

Two ways in, because they fetch differently. Opened from a class's Register menu the form finds the answer the menu already fetched: ONE ``_types`` request (what the first fix changed, and the only thing that
makes the race go away there; a held request never fires on that path). Opened from Platform > Providers (the "all" view, whose Register menu fetches nothing) the form fetches the answer itself, so that path
is where the held request is: Save must not be offered, and a loading line must be shown, until it arrives. A refused Save is said in the form, Save keeps the focus while the request is out and after the
refusal (a button that turns disabled while it has the focus drops it to ``<body>`` outside the dialog, and Tab then walks the page behind the scrim), and a Try again that works moves the focus into the
form, because the button it was on is gone. The shared cache entry also has a cost, covered here: a menu request that FAILED reaches the forms opened after it, and they must fetch again.

A request is held until the test releases it (``_Hold``), never for a fixed time: a pause is a flake on a slow runner. The focus is read from the MODAL (``.modal[role=dialog]``), not from the first
``[role=dialog]`` in the page: on the class view the overlay behind the modal is a dialog too.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-06", status="partial")

# POST /v1/llm_providers with no "limits", as the server answered it
LIMITS_422 = {
    "type": "/errors/validation-error", "title": "Validation Error", "status": 422,
    "detail": "One or more request parameters or body fields failed validation.", "instance": "/v1/llm_providers",
    "extensions": {"errors": [{"type": "missing", "loc": ["body", "limits"], "msg": "Field required"}], "request_id": "req-0aea9d32c0e9"},
}
TYPES_500 = {"type": "/errors/internal", "title": "Internal Server Error", "status": 500, "detail": "types unavailable", "instance": "/v1/llm_providers/_types"}

# where the focus is: the data-testid of the focused element, whether it is inside the MODAL, whether the page body has it, whether it is in the Name field
_FOCUS = """() => { const a = document.activeElement, d = document.querySelector('.modal[role=dialog], .sheet[role=dialog]');
  return { testid: a && a.getAttribute('data-testid'), inDialog: !!(d && a && d.contains(a)), isBody: a === document.body, inName: !!(a && a.closest('[data-field="id"]')) }; }"""


class _Hold:
    """Requests held until the test releases them. The handler waits in small steps (a fixed pause is a flake on a slow runner); the cap only keeps a broken test from hanging."""

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


def _open_the_anthropic_form(page: Page, console_url: str):
    """From the class's own Register menu (its menu fetches ``_types`` and the form finds the answer)."""
    open_legacy_route(page, console_url, "providers/llm")
    page.get_by_test_id("provider-register-toggle").click()
    page.get_by_test_id("provider-register-kind-anthropic").click()
    form = page.get_by_test_id("provider-form-llm_providers")
    form.wait_for(state="visible", timeout=15_000)
    return form


def _open_the_llm_form_from_the_all_view(page: Page):
    """From Platform > Providers (the 'all' view): its Register menu fetches nothing, so the form fetches ``_types`` itself."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    page.get_by_test_id("nv-go-platform").click()
    page.get_by_test_id("nv-plat-row:providers").click()
    expect(page.get_by_test_id("nv-plat-page:providers")).to_be_visible(timeout=15_000)
    page.get_by_test_id("provider-register-all-toggle").click()
    page.get_by_test_id("provider-register-type-llm").click()
    form = page.get_by_test_id("provider-form-llm_providers")
    form.wait_for(state="visible", timeout=15_000)
    return form


def _tab_around(page: Page, key: str, steps: int = 14) -> None:
    """More presses than the modal has controls, so the focus goes round the whole cycle: it never leaves the modal for the page behind the scrim."""
    for _ in range(steps):
        page.keyboard.press(key)
        state = page.evaluate(_FOCUS)
        assert state["inDialog"], f"{key} left the modal: {state}"


@pytest.mark.ui_e2e
def test_a_form_opened_from_the_register_menu_asks_for_the_types_once(page: Page, console_url: str) -> None:
    """The menu fetched ``_types`` under one cache key and the form under another, so the form asked again and had nothing until the second answer. Counted in the browser, once the Limits are drawn and a
    moment has passed: exactly one request."""
    seen: list[str] = []
    page.on("request", lambda request: seen.append(request.url) if request.url.endswith("/v1/llm_providers/_types") else None)
    form = _open_the_anthropic_form(page, console_url)
    expect(form.get_by_test_id("provider-form-limits")).to_be_visible(timeout=15_000)
    page.wait_for_timeout(1_500)
    assert len(seen) == 1, f"the menu and the form asked for the kind's fields {len(seen)} times: {seen}"


@pytest.mark.ui_e2e
def test_save_is_never_offered_while_the_kinds_fields_are_still_loading(page: Page) -> None:
    """The form opened from Platform > Providers asks for ``_types`` itself; the request is held until the form has said it is loading. At no moment may Save be enabled while the kind's own fields (the
    Limits box the LLM class requires) are not on screen."""
    hold = _Hold(page)

    def types(route) -> None:
        hold.wait()
        route.continue_()

    page.route("**/v1/llm_providers/_types", types)
    try:
        form = _open_the_llm_form_from_the_all_view(page)
        # the Name is the one required field that does not depend on the kind, so an operator (or a test) that types it at once has made Save look ready while the rest is still on its way
        form.locator('[data-field="id"] input').fill("journey-save-gate")
        save, limits, loading = form.get_by_test_id("provider-form-save"), form.get_by_test_id("provider-form-limits"), form.get_by_test_id("provider-form-loading")
        said_loading = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            offered = save.is_enabled()
            drawn = limits.count() > 0
            said_loading = said_loading or loading.count() > 0
            assert not (offered and not drawn), "Save was offered before the kind's fields had loaded"
            if drawn:
                break
            if said_loading:
                hold.release()                      # the form has said it is waiting: now the answer may arrive
            page.wait_for_timeout(40)
        hold.release()
        expect(limits).to_be_visible(timeout=15_000)
        assert hold.arrived == 1, "the form's own request was not the one held, so this case did not hold anything"
        assert said_loading, "the form did not say that its fields were loading"
        expect(loading).to_have_count(0)
    finally:
        hold.release()
        page.unroute_all(behavior="ignoreErrors")     # a request still held when the test ends is not a second failure


@pytest.mark.ui_e2e
def test_a_save_the_server_refuses_is_said_in_the_form_and_the_focus_never_leaves_the_modal(page: Page, console_url: str) -> None:
    """The POST is held, then answers the 422 the server answered for a missing ``limits``. While it is out Save is ``aria-busy`` and ``aria-disabled`` but not ``disabled`` and keeps the focus; a second
    Enter sends nothing; when the refusal lands the modal stays open, says what was refused in a ``role="alert"`` line, Save still has the focus, Tab and Shift+Tab go round the whole modal without leaving
    it, and the page raised no error."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    hold = _Hold(page)

    def refuse(route) -> None:
        if route.request.method == "POST":
            hold.wait()
            route.fulfill(status=422, content_type="application/problem+json", body=json.dumps(LIMITS_422))
        else:
            route.continue_()

    page.route("**/v1/llm_providers", refuse)
    try:
        form = _open_the_anthropic_form(page, console_url)
        expect(form.get_by_test_id("provider-form-limits")).to_be_visible(timeout=15_000)
        form.locator('[data-field="id"] input').fill("journey-save-refused")
        save = form.get_by_test_id("provider-form-save")
        expect(save).to_be_enabled()
        save.focus()
        page.keyboard.press("Enter")
        expect(save).to_have_attribute("aria-busy", "true")
        expect(save).to_have_attribute("aria-disabled", "true")
        assert save.evaluate("el => el.disabled") is False, "Save turned disabled while it had the focus"     # Locator.is_disabled() also counts aria-disabled
        busy = page.evaluate(_FOCUS)
        assert busy["testid"] == "provider-form-save" and busy["inDialog"], f"the focus left Save while the request was out: {busy}"
        page.keyboard.press("Enter")            # a second activation while the request is out
        hold.release()
        alert = form.get_by_test_id("provider-form-save-error")
        expect(alert).to_be_visible(timeout=10_000)
        expect(alert).to_have_attribute("role", "alert")
        expect(alert).to_contain_text("Field required")
        expect(form).to_be_visible()
        expect(save).to_be_enabled()
        expect(save).not_to_have_attribute("aria-busy", "true")
        refused = page.evaluate(_FOCUS)
        assert refused["testid"] == "provider-form-save" and refused["inDialog"] and not refused["isBody"], f"after the refusal the focus is not on Save in the modal: {refused}"
        _tab_around(page, "Tab")
        _tab_around(page, "Shift+Tab")
        page.wait_for_timeout(300)
        assert hold.arrived == 1, f"{hold.arrived} POSTs were sent for one Save"
        assert errors == [], errors
    finally:
        hold.release()
        page.unroute_all(behavior="ignoreErrors")


@pytest.mark.ui_e2e
def test_a_try_again_that_works_says_it_is_trying_and_moves_the_focus_into_the_form(page: Page) -> None:
    """The first ``_types`` answer is a 500: the form says so in an alert, with Try again after it (not inside it). Try again is pressed from the keyboard; while the retry is out it says "Trying again", is
    ``aria-busy`` and keeps the focus; when the answer arrives the button is gone, so the focus goes to the form's first field (the Name) and not to ``<body>``."""
    hold = _Hold(page)
    calls = {"n": 0}

    def flaky(route) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(status=500, content_type="application/problem+json", body=json.dumps(TYPES_500))
        else:
            hold.wait()
            route.continue_()

    page.route("**/v1/llm_providers/_types", flaky)
    try:
        form = _open_the_llm_form_from_the_all_view(page)
        alert = form.get_by_test_id("provider-form-types-error")
        expect(alert).to_be_visible(timeout=15_000)
        expect(alert).to_have_attribute("role", "alert")
        expect(alert).to_contain_text("types unavailable")
        expect(alert.get_by_test_id("provider-form-types-retry")).to_have_count(0)
        retry = form.get_by_test_id("provider-form-types-retry")
        retry.focus()
        page.keyboard.press("Enter")
        expect(retry).to_have_attribute("aria-busy", "true")
        expect(retry).to_contain_text("Trying again")
        assert page.evaluate(_FOCUS)["testid"] == "provider-form-types-retry", "the focus left Try again while the retry was out"
        hold.release()
        expect(form.get_by_test_id("provider-form-limits")).to_be_visible(timeout=15_000)
        expect(alert).to_have_count(0)
        landed = page.evaluate(_FOCUS)
        assert landed["inDialog"] and not landed["isBody"] and landed["inName"], f"after a retry that worked the focus is not in the form's first field: {landed}"
        assert calls["n"] == 2
    finally:
        hold.release()
        page.unroute_all(behavior="ignoreErrors")


@pytest.mark.ui_e2e
def test_a_try_again_whose_answer_names_no_kind_still_moves_the_focus_into_the_form(page: Page) -> None:
    """The retry is answered, but with a map that does not name the draft's kind (here: no kinds at all), so the form says it serves no kind and the fields never come. Try again goes away all the same, and
    the focus must not be left on ``<body>``."""
    hold = _Hold(page)
    calls = {"n": 0}

    def flaky(route) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(status=500, content_type="application/problem+json", body=json.dumps(TYPES_500))
        else:
            hold.wait()
            route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/v1/llm_providers/_types", flaky)
    try:
        form = _open_the_llm_form_from_the_all_view(page)
        expect(form.get_by_test_id("provider-form-types-error")).to_be_visible(timeout=15_000)
        retry = form.get_by_test_id("provider-form-types-retry")
        retry.focus()
        page.keyboard.press("Enter")
        expect(retry).to_have_attribute("aria-busy", "true")
        hold.release()
        expect(form.get_by_test_id("provider-form-kind-missing")).to_be_visible(timeout=15_000)
        expect(retry).to_have_count(0)
        landed = page.evaluate(_FOCUS)
        assert landed["inDialog"] and not landed["isBody"] and landed["inName"], f"after a retry that named no kind the focus is not in the form's first field: {landed}"
    finally:
        hold.release()
        page.unroute_all(behavior="ignoreErrors")


@pytest.mark.ui_e2e
def test_a_form_opened_after_the_menus_types_request_failed_fetches_its_fields_again(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    """The Register menu and the form share one cache entry. The class view's menu asks for ``_types`` when the page opens and that request FAILS (a 500, once); an entry in error is not fetched again by
    anything that merely joins it, so a form opened afterwards (here: an existing row opened for edit) used to show the menu's old error with Save off and never ask. It asks once when it opens on a failed
    entry, and its fields load."""
    provider_id = f"journey-types-failed-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        client.post("/v1/llm_providers", json={"id": provider_id, "provider": "anthropic", "config": {"api_key": "sk-journey"}, "limits": {"max_concurrency": 1}}).raise_for_status()
    calls = {"n": 0}

    def flaky(route) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(status=500, content_type="application/problem+json", body=json.dumps(TYPES_500))
        else:
            route.continue_()

    page.route("**/v1/llm_providers/_types", flaky)
    try:
        open_legacy_route(page, console_url, "providers/llm")
        card = page.get_by_test_id(f"provider-card-open-{provider_id}")
        expect(card).to_be_visible(timeout=15_000)
        deadline = time.monotonic() + 15
        while calls["n"] < 1 and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert calls["n"] == 1, "the menu's request did not happen, so there is no failed entry to join"
        card.click()
        form = page.get_by_test_id("provider-form-llm_providers")
        form.wait_for(state="visible", timeout=15_000)
        expect(form.get_by_test_id("provider-form-limits")).to_be_visible(timeout=15_000)
        expect(form.get_by_test_id("provider-form-types-error")).to_have_count(0)
        expect(form.get_by_test_id("provider-form-save")).to_be_enabled()
        assert calls["n"] == 2, f"the form that joined the failed entry should have asked once: {calls['n']} requests"
    finally:
        page.unroute_all(behavior="ignoreErrors")
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            client.delete(f"/v1/llm_providers/{provider_id}")
