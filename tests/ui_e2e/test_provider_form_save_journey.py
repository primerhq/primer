"""Journeys: the provider form never offers Save before the kind's fields have loaded, a Save the server refuses is said in the form, and the focus stays in the modal throughout (board task 01a12350-792d).

Retargeted for Plan 02: uses the Web Search form in the register overlay (the PC_ProviderForm path).
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._studio_helpers import open_provider_catalog

pytestmark = smk("SMK-UI-06", status="partial")

RESERVED_409 = {
    "type": "/errors/conflict",
    "title": "Conflict",
    "status": 409,
    "detail": "provider id 'DuckDuckGo' is reserved",
    "instance": "/v1/web_search_providers/DuckDuckGo",
}
TYPES_500 = {
    "type": "/errors/internal",
    "title": "Internal Server Error",
    "status": 500,
    "detail": "types unavailable",
    "instance": "/v1/web_search_providers/_types",
}

_FOCUS = """() => { const a = document.activeElement, d = document.querySelector('.modal[role=dialog], .sheet[role=dialog]');
  return { testid: a && a.getAttribute('data-testid'), inDialog: !!(d && a && d.contains(a)), isBody: a === document.body, inName: !!(a && a.closest('[data-testid="pc-field:id"], [data-field="id"]')) }; }"""


class _Hold:
    """Requests held until the test releases them."""

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


def _open_the_web_search_form(page: Page, console_url: str):
    """From Platform > Providers > web_search: click + Register to open the overlay."""
    open_provider_catalog(page, console_url, cls="web_search")
    page.get_by_test_id("pc-register-open").click()
    overlay = page.get_by_test_id("pc-register")
    overlay.wait_for(state="visible", timeout=15_000)
    return overlay


def _tab_around(page: Page, key: str, steps: int = 14) -> None:
    """More presses than the modal has controls, so the focus goes round the whole cycle."""
    for _ in range(steps):
        page.keyboard.press(key)
        state = page.evaluate(_FOCUS)
        assert state["inDialog"], f"{key} left the modal: {state}"


@pytest.mark.ui_e2e
def test_a_form_opened_from_the_register_menu_asks_for_the_types_once(page: Page, console_url: str) -> None:
    seen: list[str] = []
    page.on("request", lambda request: seen.append(request.url) if request.url.endswith("/v1/web_search_providers/_types") else None)
    overlay = _open_the_web_search_form(page, console_url)
    expect(overlay.get_by_test_id("provider-form-save")).to_be_visible(timeout=15_000)
    page.wait_for_timeout(1_500)
    assert len(seen) == 1, f"the form asked for the kind's fields {len(seen)} times: {seen}"


@pytest.mark.ui_e2e
def test_save_is_never_offered_while_the_kinds_fields_are_still_loading(page: Page, console_url: str) -> None:
    hold = _Hold(page)

    def types(route) -> None:
        hold.wait()
        route.continue_()

    page.route("**/v1/web_search_providers/_types", types)
    try:
        overlay = _open_the_web_search_form(page, console_url)
        overlay.get_by_test_id("pc-field:id").fill("journey-save-gate")
        save = overlay.get_by_test_id("provider-form-save")
        loading = overlay.get_by_test_id("provider-form-loading")
        said_loading = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            offered = save.is_enabled()
            said_loading = said_loading or loading.count() > 0
            assert not (offered and loading.count() > 0), "Save was offered before the kind's fields had loaded"
            if loading.count() == 0 and said_loading:
                break
            if said_loading:
                hold.release()
            page.wait_for_timeout(40)
        hold.release()
        expect(save).to_be_visible(timeout=15_000)
        assert hold.arrived == 1, "the form's request was not held"
        assert said_loading, "the form did not say that its fields were loading"
        expect(loading).to_have_count(0)
    finally:
        hold.release()
        page.unroute_all(behavior="ignoreErrors")


@pytest.mark.ui_e2e
def test_a_save_the_server_refuses_is_said_in_the_form_and_the_focus_never_leaves_the_modal(page: Page, console_url: str) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    hold = _Hold(page)

    def refuse(route) -> None:
        if route.request.method == "POST":
            hold.wait()
            route.fulfill(status=409, content_type="application/problem+json", body=json.dumps(RESERVED_409))
        else:
            route.continue_()

    page.route("**/v1/web_search_providers", refuse)
    try:
        overlay = _open_the_web_search_form(page, console_url)
        expect(overlay.get_by_test_id("provider-form-save")).to_be_visible(timeout=15_000)
        overlay.get_by_test_id("pc-field:id").fill("DuckDuckGo")
        save = overlay.get_by_test_id("provider-form-save")
        expect(save).to_be_enabled()
        save.focus()
        page.keyboard.press("Enter")
        expect(save).to_have_attribute("aria-busy", "true")
        expect(save).to_have_attribute("aria-disabled", "true")
        assert save.evaluate("el => el.disabled") is False, "Save turned disabled while it had the focus"
        busy = page.evaluate(_FOCUS)
        assert busy["testid"] == "provider-form-save" and busy["inDialog"], f"the focus left Save while the request was out: {busy}"
        page.keyboard.press("Enter")
        hold.release()
        alert = overlay.get_by_test_id("pc-register-error")
        expect(alert).to_be_visible(timeout=10_000)
        expect(alert).to_have_attribute("role", "alert")
        expect(alert).to_contain_text("DuckDuckGo")
        expect(overlay).to_be_visible()
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
def test_a_try_again_that_works_says_it_is_trying_and_moves_the_focus_into_the_form(page: Page, console_url: str) -> None:
    hold = _Hold(page)
    calls = {"n": 0}

    def flaky(route) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(status=500, content_type="application/problem+json", body=json.dumps(TYPES_500))
        else:
            hold.wait()
            route.continue_()

    page.route("**/v1/web_search_providers/_types", flaky)
    try:
        overlay = _open_the_web_search_form(page, console_url)
        alert = overlay.get_by_test_id("provider-form-types-error")
        expect(alert).to_be_visible(timeout=15_000)
        expect(alert).to_have_attribute("role", "alert")
        expect(alert).to_contain_text("types unavailable")
        expect(alert.get_by_test_id("provider-form-types-retry")).to_have_count(0)
        retry = overlay.get_by_test_id("provider-form-types-retry")
        retry.focus()
        page.keyboard.press("Enter")
        expect(retry).to_have_attribute("aria-busy", "true")
        expect(retry).to_contain_text("Trying again")
        assert page.evaluate(_FOCUS)["testid"] == "provider-form-types-retry", "the focus left Try again while the retry was out"
        hold.release()
        expect(overlay.get_by_test_id("provider-form-save")).to_be_visible(timeout=15_000)
        expect(alert).to_have_count(0)
        landed = page.evaluate(_FOCUS)
        assert landed["inDialog"] and not landed["isBody"] and landed["inName"], f"after a retry that worked the focus is not in the form's first field: {landed}"
        assert calls["n"] == 2
    finally:
        hold.release()
        page.unroute_all(behavior="ignoreErrors")


@pytest.mark.ui_e2e
def test_a_try_again_whose_answer_names_no_kind_still_moves_the_focus_into_the_form(page: Page, console_url: str) -> None:
    hold = _Hold(page)
    calls = {"n": 0}

    def flaky(route) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(status=500, content_type="application/problem+json", body=json.dumps(TYPES_500))
        else:
            hold.wait()
            route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/v1/web_search_providers/_types", flaky)
    try:
        overlay = _open_the_web_search_form(page, console_url)
        expect(overlay.get_by_test_id("provider-form-types-error")).to_be_visible(timeout=15_000)
        retry = overlay.get_by_test_id("provider-form-types-retry")
        retry.focus()
        page.keyboard.press("Enter")
        expect(retry).to_have_attribute("aria-busy", "true")
        hold.release()
        expect(overlay.get_by_test_id("provider-form-kind-missing")).to_be_visible(timeout=15_000)
        expect(retry).to_have_count(0)
        landed = page.evaluate(_FOCUS)
        assert landed["inDialog"] and not landed["isBody"] and landed["inName"], f"after a retry that named no kind the focus is not in the form's first field: {landed}"
    finally:
        hold.release()
        page.unroute_all(behavior="ignoreErrors")


@pytest.mark.ui_e2e
def test_a_form_opened_after_the_menus_types_request_failed_fetches_its_fields_again(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    provider_id = f"journey-ws-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        client.post(
            "/v1/web_search_providers",
            json={"id": provider_id, "provider_type": "tavily", "config": {"type": "tavily", "api_key": "tvly-test"}},
        ).raise_for_status()
    calls = {"n": 0}

    def flaky(route) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(status=500, content_type="application/problem+json", body=json.dumps(TYPES_500))
        else:
            route.continue_()

    page.route("**/v1/web_search_providers/_types", flaky)
    try:
        open_provider_catalog(page, console_url, cls="web_search")
        card = page.get_by_test_id(f"pk-card:web_search:{provider_id}")
        expect(card).to_be_visible(timeout=15_000)
        card.click()
        overlay = page.get_by_test_id("pc-register")
        overlay.wait_for(state="visible", timeout=15_000)
        expect(overlay.get_by_test_id("provider-form-save")).to_be_visible(timeout=15_000)
        expect(overlay.get_by_test_id("provider-form-types-error")).to_have_count(0)
        assert calls["n"] >= 1
    finally:
        page.unroute_all(behavior="ignoreErrors")
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            client.delete(f"/v1/web_search_providers/{provider_id}")
