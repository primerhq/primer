"""Journey: every control of the channel provider and channel forms has a name (console review C-003, the channels surface).

The forms drew their labels as siblings of the controls: ``input.labels`` was empty for every field. In the real modals each visible input, select and textarea now has a label
(or an ``aria-label``), whichever platform's config fields are showing, and a click on a label focuses its control.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-06", status="partial")

_UNNAMED = """els => els.filter(e => e.type !== 'hidden' && e.offsetParent !== null
    && e.labels.length === 0 && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby'))
    .map(e => e.outerHTML.slice(0, 90))"""


def _unnamed(modal) -> list[str]:
    return modal.locator("input, select, textarea").evaluate_all(_UNNAMED)


@pytest.mark.parametrize("platform", ["slack", "telegram", "discord"])
def test_every_control_of_the_provider_form_is_named_whichever_platform_is_showing(page: Page, console_url: str, platform: str) -> None:
    open_legacy_route(page, console_url, "channels/providers")
    page.get_by_role("button", name="New provider", exact=True).click()
    modal = page.locator(".modal").first
    expect(modal).to_be_visible(timeout=10_000)
    modal.locator("select").first.select_option(platform)
    expect(modal.locator("input").nth(1)).to_be_visible(timeout=5_000)

    assert _unnamed(modal) == []
    # a click on the visible label focuses its control
    modal.locator("label.field-label", has_text="id").first.click()
    expect(modal.get_by_placeholder("auto-generated").first).to_be_focused()


def test_every_control_of_the_channel_form_is_named(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    """The New channel button stays disabled until a provider exists, so one is seeded (and removed)."""
    provider_id = f"cp-lbl-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/channel_providers", json={"id": provider_id, "provider": "discord", "config": {"bot_token": "x" * 60}})
        assert r.status_code == 201, r.text
    try:
        open_legacy_route(page, console_url, "channels")
        new_channel = page.get_by_role("button", name="New channel").first
        expect(new_channel).to_be_enabled(timeout=20_000)
        new_channel.click()
        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=10_000)
        expect(modal.locator("select").first).to_be_visible(timeout=5_000)
        assert _unnamed(modal) == []
        # the format note under the external id is the row's help line: drawn, and tied to the input (it vanished when the row component had no help line)
        external = modal.get_by_placeholder("C0123ABC456 / chat-id / snowflake")
        described_by = external.get_attribute("aria-describedby")
        assert described_by, "the external id's format note does not describe its input"
        expect(modal.locator(f"[id='{described_by.split()[0]}']")).to_contain_text("Telegram: chat ID")
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/channel_providers/{provider_id}")
