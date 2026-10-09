"""Journey: the Create session overlay says where files go (ticket 01a11dd7-fc79).

In the real overlay the ``Initial instructions`` field is described by a help line that tells the user to create the session parked and attach files to the first message. The overlay
has no file picker, so without the line there is no hint that files are possible at all.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_overlay, open_shell

pytestmark = smk("SMK-UI-06", status="partial")

_SENTENCE = "To attach files, create the session parked and attach them to your first message."


def _first_workspace(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        items = c.get("/v1/workspaces").json()["items"]
    assert items, "the install has a default workspace"
    return items[0]["id"]


@pytest.mark.ui_e2e
def test_the_instructions_field_is_described_by_the_attach_sentence(page: Page, base_url: str, console_url: str) -> None:
    wid = _first_workspace(base_url)
    open_shell(page, console_url, wid)
    open_overlay(page, console_url, wid, "new-session")
    panel = page.get_by_test_id("nv-overlay:new-session")

    instructions = panel.get_by_test_id("nv-ns-instr")
    expect(instructions).to_be_visible(timeout=10_000)
    described_by = instructions.get_attribute("aria-describedby")
    assert described_by, "the instructions field has no description"
    expect(panel.locator(f"[id='{described_by.split()[0]}']")).to_have_text(_SENTENCE)
    # the line is on screen under the field, not only in the accessibility tree
    expect(panel.get_by_text(_SENTENCE, exact=True)).to_be_visible()
