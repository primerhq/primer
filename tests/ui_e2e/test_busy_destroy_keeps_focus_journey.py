"""Journey: the Destroy workspace confirm stays open and keeps the focus while its DELETE is out (review of #732, B2).

``onConfirm`` closed the confirm BEFORE it sent the request, so ``busy`` on "Destroy permanently" never rendered, and the trap sent the focus back to the opener "Destroy workspace", which was natively disabled for the
length of the request: focus on ``<body>`` for all of it. The confirm stays open while the request is out (its button is busy and keeps the focus, a second Enter sends nothing, Tab stays in the dialog); a refusal closes
it onto the cascade Banner and the opener has the focus back, enabled. The DELETE is held by the test, so it does not race the server.
"""

from __future__ import annotations

import json

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._studio_helpers import open_workspace_settings
from tests.ui_e2e.test_workspace_session_graph_signals import _cleanup, _seed_workspace

_WHERE = """() => {
  const m = document.querySelector('.modal'), a = document.activeElement;
  return { inside: !!(m && a && m.contains(a)), body: a === document.body, busy: a && a.getAttribute ? a.getAttribute('aria-busy') : null, disabled: !!(a && a.disabled),
           text: a && a.textContent ? a.textContent.trim() : null };
}"""


@pytest.mark.ui_e2e
def test_the_destroy_confirm_stays_open_and_keeps_the_focus_while_the_request_is_out(page: Page, base_url: str, console_url: str, unique_suffix: str, tmp_path) -> None:
    wp_id, tpl_id = f"wp-bd-{unique_suffix}", f"tpl-bd-{unique_suffix}"
    wid = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
    held: list = []
    page.route(f"**/v1/workspaces/{wid}", lambda route: held.append(route) if route.request.method == "DELETE" else route.continue_())
    try:
        open_workspace_settings(page, console_url, wid, "destroy")
        opener = page.get_by_role("button", name="Destroy workspace", exact=True).first
        opener.wait_for(state="visible", timeout=10_000)
        opener.click()
        confirm = page.get_by_role("button", name="Destroy permanently", exact=True).first
        expect(confirm).to_be_visible(timeout=5_000)
        confirm.focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)                                      # Chromium hands the focus of a disabled control to <body> within about 50 ms
        assert len(held) == 1, f"the DELETE was sent {len(held)} times"
        expect(confirm).to_be_visible(timeout=5_000)              # the confirm is still there while the request is out ...
        state = page.evaluate(_WHERE)
        assert state["inside"] and not state["body"], f"the focus left the confirm when the request went out: {state}"
        assert state["busy"] == "true" and not state["disabled"] and state["text"] == "Destroy permanently", f"the focused button is not the busy one: {state}"

        page.keyboard.press("Enter")                                    # ... a second Enter sends nothing ...
        page.wait_for_timeout(300)
        assert len(held) == 1, f"a second Enter on the busy button sent another DELETE ({len(held)})"
        for key in ("Tab", "Tab", "Shift+Tab", "Shift+Tab"):          # ... and Tab stays inside it
            page.keyboard.press(key)
            state = page.evaluate(_WHERE)
            assert state["inside"], f"{key} with the button busy left the confirm: {state}"

        # the server refuses: the confirm closes onto the cascade Banner and the opener has the focus back, enabled
        held[0].fulfill(status=409, content_type="application/problem+json",
                        body=json.dumps({"type": "about:blank", "title": "Destroy blocked", "status": 409, "detail": "sessions are still running"}))
        expect(page.get_by_text("sessions are still running", exact=False).first).to_be_visible(timeout=10_000)
        expect(page.get_by_role("button", name="Destroy permanently", exact=True)).to_have_count(0)
        refocused = page.evaluate("() => { const a = document.activeElement; return { text: a && a.textContent ? a.textContent.trim() : null, disabled: !!(a && a.disabled) }; }")
        assert refocused == {"text": "Destroy workspace", "disabled": False}, f"the opener does not have the focus back: {refocused}"
    finally:
        for route in held:
            try:
                route.continue_()
            except Exception:  # noqa: BLE001 - already fulfilled
                pass
        page.unroute_all(behavior="ignoreErrors")
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            client.delete(f"/v1/workspaces/{wid}")
        _cleanup(base_url, [f"/v1/workspace_templates/{tpl_id}", f"/v1/workspace_providers/{wp_id}"])
