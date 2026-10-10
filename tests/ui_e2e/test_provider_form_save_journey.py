"""Journey: the provider form never offers Save before the kind's fields have loaded, and a Save the server refuses is said in the form (board task 01a12350-792d).

Found on main by the lead: ``test_u0047_provider_list_reflects_new_row_after_modal_create`` failed on main 919f3f28 and on #700 and passed on neighbouring commits. The form fetched ``GET /<plural>/_types`` a
second time (under another cache key than the Register menu), and until that answer arrived it offered an enabled Save with no Limits and no API key box; the POST had no ``limits`` and answered 422, which the
catalog's ``save()`` let go as an unhandled rejection: the modal stayed open and said nothing. These cases hold the answer to the form back (the request is delayed, so the race is not left to the timing of a CI
runner) and make the server refuse the Save with the 422 it really answered.
"""

from __future__ import annotations

import json
import time

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


def _open_the_anthropic_form(page: Page, console_url: str):
    open_legacy_route(page, console_url, "providers/llm")
    page.get_by_test_id("provider-register-toggle").click()
    page.get_by_test_id("provider-register-kind-anthropic").click()
    form = page.get_by_test_id("provider-form-llm_providers")
    form.wait_for(state="visible", timeout=15_000)
    return form


@pytest.mark.ui_e2e
def test_save_is_never_offered_while_the_kinds_fields_are_still_loading(page: Page, console_url: str) -> None:
    """Every ``_types`` request after the first (the Register menu's, made when the page opens) is held for 2.5 s, so a form that asks for its own copy has nothing for that long. At no moment may Save be
    enabled while the kind's own fields (the Limits box every class has) are not on screen."""
    asked = {"n": 0}

    def types(route) -> None:
        asked["n"] += 1
        if asked["n"] > 1:
            page.wait_for_timeout(2_500)
        route.continue_()

    page.route("**/v1/llm_providers/_types", types)
    try:
        form = _open_the_anthropic_form(page, console_url)
        # the Name is the one required field that does not depend on the kind, so an operator (or a test) that types it at once has made Save look ready while the rest is still on its way
        form.locator('[data-field="id"] input').fill("journey-save-gate")
        save, limits = form.get_by_test_id("provider-form-save"), form.get_by_test_id("provider-form-limits")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            offered = save.is_enabled()
            drawn = limits.count() > 0
            assert not (offered and not drawn), "Save was offered before the kind's fields had loaded"
            if drawn:
                break
            page.wait_for_timeout(40)
        expect(limits).to_be_visible(timeout=15_000)
        expect(save).to_be_enabled()
    finally:
        page.unroute_all(behavior="ignoreErrors")     # a request still held when the test ends is not a second failure


@pytest.mark.ui_e2e
def test_a_save_the_server_refuses_is_said_in_the_form_and_raises_nothing(page: Page, console_url: str) -> None:
    """The POST answers the 422 the server answered for a missing ``limits``. The modal stays open, says what was refused in a ``role="alert"`` line, Save works again, and the page raised no error."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    def refuse(route) -> None:
        if route.request.method == "POST":
            route.fulfill(status=422, content_type="application/problem+json", body=json.dumps(LIMITS_422))
        else:
            route.continue_()

    page.route("**/v1/llm_providers", refuse)
    form = _open_the_anthropic_form(page, console_url)
    expect(form.get_by_test_id("provider-form-limits")).to_be_visible(timeout=15_000)
    form.locator('[data-field="id"] input').fill("journey-save-refused")
    save = form.get_by_test_id("provider-form-save")
    expect(save).to_be_enabled()
    save.click()
    alert = form.get_by_test_id("provider-form-save-error")
    expect(alert).to_be_visible(timeout=10_000)
    expect(alert).to_have_attribute("role", "alert")
    expect(alert).to_contain_text("Field required")
    expect(form).to_be_visible()
    expect(save).to_be_enabled()
    page.wait_for_timeout(300)
    assert errors == [], errors
