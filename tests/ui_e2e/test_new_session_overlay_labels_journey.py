"""Journey: the New session overlay's fields are named by their labels (console review C-003, the console overlays).

``NV_Field`` drew every overlay field's label as ``<div class="nv-field-label">``: no ``<label>`` anywhere in the overlay, so ``input.labels`` was empty, a click on the visible
text focused nothing and a screen reader met unnamed fields. In the real overlay the name and instructions fields are now found by their label, a click on the label focuses
them, and the fields around a custom widget (the agent/graph picker, the autonomy switch) are groups named by their label.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_overlay, open_shell

pytestmark = smk("SMK-UI-06", status="partial")


def _first_workspace(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        items = c.get("/v1/workspaces").json()["items"]
    assert items, "the install has a default workspace"
    return items[0]["id"]


@pytest.mark.ui_e2e
def test_the_overlay_fields_are_found_by_their_label_and_a_label_click_focuses_them(page: Page, base_url: str, console_url: str) -> None:
    wid = _first_workspace(base_url)
    open_shell(page, console_url, wid)
    open_overlay(page, console_url, wid, "new-session")
    panel = page.get_by_test_id("nv-overlay:new-session")

    name = panel.get_by_label("Name")
    expect(name).to_have_count(1)
    # the hint is a word of its own in the field's name ("Name optional", not "Nameoptional")
    expect(panel.get_by_label("Name optional", exact=True)).to_have_count(1)
    name.fill("a session named by its label")
    expect(panel.get_by_test_id("nv-ns-name")).to_have_value("a session named by its label")

    panel.locator("label.nv-field-label", has_text="Initial instructions").click()
    expect(panel.get_by_test_id("nv-ns-instr")).to_be_focused()
    expect(panel.get_by_label("Initial instructions")).to_have_count(1)

    expect(panel.get_by_role("group", name="Bind to an agent or a graph")).to_have_count(1)
    panel.get_by_test_id("nv-ns-adv").click()
    expect(panel.get_by_role("group", name="Autonomy")).to_have_count(1)
