"""Journey: the Create button of a form dialog keeps the focus while its POST is out (board task 01a12480-2176, PR 2).

``<Btn disabled={busy || !name.trim() || ...}>`` turned the focused "Create" natively disabled the moment Enter was pressed, and Chromium moves the focus of a disabled control to ``<body>``: the keyboard user
lost their place in the dialog and the next Tab began at the top of the page behind the scrim. The button is ``aria-disabled`` and ``aria-busy`` now (``Btn busy``): it keeps the focus, a second Enter sends
nothing, and Tab stays in the dialog. The New service dialog stands for the create/edit forms converted in this PR (it needs no other entity); the POST is held by the test, so it does not race the server.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_view

_WHERE = """() => {
  const m = document.querySelector('.modal'), a = document.activeElement;
  return { inside: !!(m && a && m.contains(a)), body: a === document.body, testid: (a && a.getAttribute && a.getAttribute('data-testid')) || null,
           busy: a && a.getAttribute ? a.getAttribute('aria-busy') : null, disabled: !!(a && a.disabled) };
}"""


def _delete_services_named(base_url: str, name: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        for row in client.get("/v1/services", params={"limit": 200}).json().get("items", []):
            if row.get("name") == name:
                client.delete(f"/v1/services/{row['id']}")


@pytest.mark.ui_e2e
def test_the_focused_create_button_keeps_the_focus_while_the_post_is_out(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    name = f"busy-{unique_suffix}"
    held: list = []
    page.route("**/v1/services", lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
    try:
        expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
        open_view(page, console_url, "primer", "platform:services")
        expect(page.get_by_test_id("nv-plat-page:services")).to_be_visible(timeout=20_000)
        page.get_by_test_id("nv-plat-create").click()
        expect(page.locator(".modal-overlay")).to_contain_text("New service", timeout=10_000)
        page.get_by_test_id("service-name-input").fill(name)
        page.get_by_test_id("service-description-input").fill(f"busy journey {unique_suffix}")
        save = page.get_by_test_id("service-save-btn")
        expect(save).to_be_enabled(timeout=5_000)
        save.focus()
        page.keyboard.press("Enter")
        page.wait_for_function("() => { const b = document.querySelector('[data-testid=service-save-btn]'); return b && (b.disabled || b.getAttribute('aria-busy') === 'true'); }", timeout=5_000)
        page.wait_for_timeout(400)                                      # Chromium hands the focus of a disabled control to <body> within about 50 ms
        state = page.evaluate(_WHERE)
        assert len(held) == 1, f"the POST was sent {len(held)} times"
        assert state["inside"] and not state["body"], f"the focus left the dialog when the request went out: {state}"
        assert state["testid"] == "service-save-btn" and state["busy"] == "true" and not state["disabled"], f"the focused button is not the busy Create: {state}"

        page.keyboard.press("Enter")                                    # a second Enter on a busy button sends nothing
        page.wait_for_timeout(300)
        assert len(held) == 1, f"a second Enter on the busy button sent another POST ({len(held)})"

        for key in ("Tab", "Tab", "Shift+Tab", "Shift+Tab"):
            page.keyboard.press(key)
            state = page.evaluate(_WHERE)
            assert state["inside"], f"{key} with the button busy left the dialog for the page behind it: {state}"
    finally:
        for route in held:
            route.continue_()
        page.wait_for_timeout(300)
        page.unroute_all(behavior="ignoreErrors")
        _delete_services_named(base_url, name)
