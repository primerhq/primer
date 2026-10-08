"""Journey: the trigger subscription dialog names its Kind and Parallelism rows (console review C-003, the triggers surface).

The two rows hold radio buttons under a bare ``<label>`` that named nothing; each is a group named by its label now, and the radios inside keep their own names.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-03", status="partial")


def test_the_add_subscription_dialog_rows_are_groups_named_by_their_label(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    slug = f"sl-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/triggers", json={"slug": slug, "name": slug, "description": None, "config": {"kind": "webhook"}, "enabled": True})
        assert r.status_code == 201, r.text
        trigger_id = r.json()["id"]
    try:
        open_legacy_route(page, console_url, f"triggers/{trigger_id}")
        page.get_by_test_id("add-subscription-btn").click()
        dialog = page.get_by_test_id("tr-sub-dialog")
        expect(dialog).to_be_visible(timeout=15_000)

        for name in ("Kind", "Parallelism"):
            expect(dialog.get_by_role("group", name=re.compile(rf"^{name}"))).to_have_count(1)
        # the radios inside a group are still found by their own words
        expect(dialog.get_by_role("radio", name=re.compile("queue"))).to_have_count(1)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/triggers/{trigger_id}")
