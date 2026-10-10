"""Journey: the confirm button of a delete dialog keeps the focus while its request is out (board task 01a12480-2176).

``<Btn disabled={del.loading}>`` turned the focused "Delete provider" natively disabled the moment Enter was pressed, and Chromium moves the focus of a disabled control to ``<body>`` (within about 50 ms): the
keyboard user lost their place in the dialog, and the next Tab began at the top of the page behind the scrim. The button is ``aria-disabled`` and ``aria-busy`` now (``Btn busy``): it keeps the focus, a second
Enter sends nothing, and Tab stays in the dialog. The DELETE is held by the test, so it does not race the server.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_legacy_route

_WHERE = """() => {
  const m = document.querySelector('.modal'), a = document.activeElement;
  return { inside: !!(m && a && m.contains(a)), body: a === document.body, busy: a && a.getAttribute ? a.getAttribute('aria-busy') : null,
           disabled: !!(a && a.disabled), text: a && a.textContent ? a.textContent.trim() : null };
}"""


def _cleanup(base_url: str, provider_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        try:
            client.delete(f"/v1/workspace_providers/{provider_id}")
        except Exception:  # noqa: BLE001 - best effort
            pass


@pytest.mark.ui_e2e
def test_the_focused_delete_button_keeps_the_focus_while_the_request_is_out(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    provider_id = f"ws-busy-{unique_suffix}"
    created = httpx.post(f"{base_url}/v1/workspace_providers", json={"id": provider_id, "provider": "local", "config": {"kind": "local", "root_path": f"/tmp/{provider_id}"}}, timeout=30.0)
    assert created.status_code in (200, 201), created.text
    held: list = []
    page.route(f"**/v1/workspace_providers/{provider_id}", lambda route: held.append(route) if route.request.method == "DELETE" else route.continue_())
    try:
        page.wait_for_function("() => typeof window.WorkspaceProvidersPage === 'function'", timeout=15_000)
        open_legacy_route(page, console_url, f"workspaces/providers/{provider_id}")
        page.get_by_role("button", name="Delete", exact=True).first.click()
        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)
        confirm = modal.get_by_role("button", name="Delete provider").first
        expect(confirm).to_be_enabled(timeout=5_000)
        confirm.focus()
        page.keyboard.press("Enter")
        page.wait_for_function("() => document.querySelector('.modal button[aria-busy=true], .modal button:disabled') !== null", timeout=5_000)
        page.wait_for_timeout(400)                                      # Chromium hands the focus of a disabled control to <body> within about 50 ms
        state = page.evaluate(_WHERE)
        assert len(held) == 1, f"the DELETE was sent {len(held)} times"
        assert state["inside"] and not state["body"], f"the focus left the dialog when the request went out: {state}"
        assert state["busy"] == "true" and not state["disabled"], f"the focused button is not the busy one: {state}"

        page.keyboard.press("Enter")                                    # a second Enter on a busy button sends nothing
        page.wait_for_timeout(300)
        assert len(held) == 1, f"a second Enter on the busy button sent another DELETE ({len(held)})"

        for key in ("Tab", "Tab", "Shift+Tab", "Shift+Tab"):
            page.keyboard.press(key)
            state = page.evaluate(_WHERE)
            assert state["inside"], f"{key} with the button busy left the dialog for the page behind it: {state}"
    finally:
        for route in held:
            route.continue_()
        page.wait_for_timeout(200)
        page.unroute_all(behavior="ignoreErrors")
        _cleanup(base_url, provider_id)
