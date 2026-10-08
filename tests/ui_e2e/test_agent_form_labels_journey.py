"""Journey: the agent form names the rows that hold a custom widget (console review C-003, the agents surface).

The model-profile picker, the system-prompt parts and the tool picker were each under a bare ``<label>`` that named nothing; each row is now a group named by its label,
and every system-prompt part is a textarea with a name of its own ("System prompt part 1", "System prompt part 2").
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-03", status="partial")


def test_the_custom_widget_rows_are_groups_named_by_their_label_and_each_prompt_part_is_named(page: Page, console_url: str) -> None:
    open_legacy_route(page, console_url, "agents")
    page.get_by_role("button", name="New agent").first.click()
    modal = page.locator(".modal").first
    modal.wait_for(state="visible", timeout=5_000)

    for name in ("Model profile", "System prompt", "Tools"):
        expect(modal.get_by_role("group", name=re.compile(rf"^{name}"))).to_have_count(1)
    # the plain inputs were already labelled by hand and stay so
    expect(modal.get_by_label("Temperature")).to_have_count(1)

    first = modal.get_by_label("System prompt part 1")
    expect(first).to_have_count(1)
    first.fill("You triage refunds.")
    modal.get_by_test_id("agent-system-prompt-add").click()
    expect(modal.get_by_label("System prompt part 2")).to_have_count(1)
    expect(modal.get_by_label("System prompt part 1")).to_have_value("You triage refunds.")
