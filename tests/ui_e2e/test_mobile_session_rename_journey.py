"""Journey: a session can be renamed on a phone (lead ask on the failed-session wording PR).

The mobile top bar names the session, so the session header's own title is hidden at that width, and the title is what
click-to-rename hangs on. The header's overflow menu offers Rename so the phone keeps the ability.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import shell_url
from tests.ui_e2e.test_failed_session_wording_journey import _seed, _start

PHONE = {"width": 390, "height": 844}


@pytest.mark.timeout(120)
def test_a_session_is_renamed_from_the_overflow_menu_on_a_phone(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    _registry, mock_base_url = mock_llm_lan
    ids = _seed(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path)
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = _start(client, ids, "rename me", auto_start=False)

    page.set_viewport_size(PHONE)
    page.goto(shell_url(console_url, ids["workspace"]) + f"?doc=session:{sid}")
    expect(page.get_by_test_id("nv-mob-session-screen")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("nv-session-title")).to_be_hidden()   # the top bar names it; nothing here to click

    page.get_by_test_id("nv-session-overflow").click()
    page.get_by_test_id("nv-session-rename").click()
    dialog_input = page.get_by_test_id("dialog-input")
    expect(dialog_input).to_be_visible(timeout=10_000)
    dialog_input.fill("Quarterly numbers")
    page.get_by_test_id("dialog-confirm").click()

    expect(page.get_by_test_id("nv-mob-session-screen").locator(".nv-mob-screen-title")).to_have_text(
        "Quarterly numbers", timeout=15_000,
    )
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        assert client.get(f"/v1/sessions/{sid}").json()["name"] == "Quarterly numbers"
