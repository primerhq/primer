"""A refused write is not also an uncaught error (finding ADM-18 of the 2026-10-08 admin review).

``useMutation`` shows the toast (or calls ``onError``) and then rethrows, because callers that ``await`` the mutation need to know it failed. Most call
sites do not await it (``create.mutate(body)`` in a click handler), so the rethrown error was an unhandled promise rejection: every server refusal of a
write, a 422 or a 409 the form handles perfectly well, also reached ``window.onerror`` / ``unhandledrejection`` and Playwright's ``pageerror``
("ApiError: Validation Error at apiFetch ... at async Object.mutate"). Anything that watches uncaught errors saw one per user mistake, which buries the real
ones.

The journey drives the two refusals of the finding through the real server and the real New-provider modal on Channels, and asserts the refusal is shown
(inline for the 422, a toast for the 409) AND that the page raised no uncaught error. ``tests/ui/test_use_mutation_handled.py`` pins the contract of the
hook itself.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests.ui_e2e._shell_helpers import open_legacy_route


def _open_new_provider_modal(page, console_url: str):
    open_legacy_route(page, console_url, "channels/providers")
    new_btn = page.get_by_role("button", name="New provider", exact=True)
    expect(new_btn).to_be_visible(timeout=20_000)
    new_btn.click()
    modal = page.locator(".modal").first
    expect(modal).to_be_visible(timeout=5_000)
    return modal


def _uncaught(console_messages: list[dict]) -> list[str]:
    return [f"{m['level']}: {m['text'][:200]}" for m in console_messages if m["level"] == "pageerror"]


def _cleanup(base_url: str, provider_ids: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for pid in provider_ids:
            try:
                c.delete(f"/v1/channel_providers/{pid}")
            except Exception:  # noqa: BLE001
                pass


def test_a_422_the_form_shows_inline_is_not_also_an_uncaught_error(page, base_url: str, console_url: str, console_messages: list[dict], unique_suffix: str) -> None:
    cp_id = f"adm18-422-{unique_suffix}"
    try:
        modal = _open_new_provider_modal(page, console_url)
        modal.get_by_placeholder("auto-generated", exact=False).first.fill(cp_id)
        tokens = modal.locator("input[type=password]")
        expect(tokens).to_have_count(3, timeout=5_000)
        tokens.nth(0).fill("wrongprefix-not-xapp")
        tokens.nth(1).fill("xoxb-test-placeholder")
        modal.get_by_role("button", name="Create provider", exact=True).click()

        # The refusal is shown, the modal stays open for a retry...
        expect(modal.get_by_text("xapp-", exact=False).last).to_be_visible(timeout=10_000)
        expect(modal).to_be_visible()
        # ...and it is not ALSO an uncaught error.
        page.wait_for_timeout(500)
        assert _uncaught(console_messages) == [], _uncaught(console_messages)
    finally:
        _cleanup(base_url, [cp_id])


def test_a_409_shown_as_a_toast_is_not_also_an_uncaught_error(page, base_url: str, console_url: str, console_messages: list[dict], unique_suffix: str) -> None:
    cp_id = f"adm18-409-{unique_suffix}"
    body = {"id": cp_id, "provider": "slack", "config": {"app_token": "xapp-test-token", "bot_token": "xoxb-test-placeholder"}}
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            first = c.post("/v1/channel_providers", json=body)
            assert first.status_code in (200, 201), first.text
        modal = _open_new_provider_modal(page, console_url)
        modal.get_by_placeholder("auto-generated", exact=False).first.fill(cp_id)
        tokens = modal.locator("input[type=password]")
        expect(tokens).to_have_count(3, timeout=5_000)
        tokens.nth(0).fill("xapp-test-token")
        tokens.nth(1).fill("xoxb-test-placeholder")
        modal.get_by_role("button", name="Create provider", exact=True).click()

        # The duplicate id is refused and the refusal is shown as an error toast (a 409 has no field to sit under): not silence.
        expect(page.locator(".toast.toast-error").first).to_be_visible(timeout=10_000)
        page.wait_for_timeout(500)
        assert _uncaught(console_messages) == [], _uncaught(console_messages)
    finally:
        _cleanup(base_url, [cp_id])
