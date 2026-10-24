"""Journey: the New rule form offers the reply targets the server takes (ticket 01a16e37-b98e, ruling 2).

``dm_sender`` is refused by the server (no channel can post to a person's direct messages), so the form does not offer it. Its default
sends no ``reply_target`` at all (null: a message in a thread or a DM is answered there, a room post through the workspace's reply
binding), and ``none`` is a real value: the session posts nothing. Lead-scheduled; never run locally.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_legacy_route


def test_the_new_rule_form_offers_default_source_thread_source_room_and_none(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    """The rules page shows its New rule button only once a provider exists, so one is seeded (and removed)."""
    provider_id = f"cp-rt-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/channel_providers", json={"id": provider_id, "provider": "discord", "config": {"bot_token": "x" * 60}})
        assert r.status_code == 201, r.text
    try:
        open_legacy_route(page, console_url, "channels/rules")
        new_rule = page.get_by_role("button", name="New rule").first
        expect(new_rule).to_be_enabled(timeout=20_000)
        new_rule.click()
        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=10_000)

        reply_target = modal.get_by_label("Reply target", exact=True)
        expect(reply_target).to_have_count(1, timeout=5_000)
        assert reply_target.locator("option").all_inner_texts() == ["default", "source_thread", "source_room", "none (post nothing)"]
        assert reply_target.locator("option").evaluate_all("os => os.map(o => o.value)") == ["", "source_thread", "source_room", "none"]
        expect(reply_target).to_have_value("")
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/channel_providers/{provider_id}")
