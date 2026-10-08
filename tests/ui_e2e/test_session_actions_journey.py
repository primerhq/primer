"""Journey: End, Park and Delete are reachable everywhere a session can be opened, and every outcome is shown
(console review 2026-10-08, C-012, C-022, C-034).

Before: End and Park lived only in a right-click rail menu (nothing on a phone, nothing in the palette), the overflow's "Close
Session" ended the session without asking, and End, Park and Delete had no failure path: deleting a running session answered 409
and the console showed nothing while the turn kept running.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.mock_llm import Rule
from tests.ui_e2e._scripted_session import seed_scripted_agent, start_session
from tests.ui_e2e._shell_helpers import run_verb, session_row, shell_url
from tests.ui_e2e._studio_helpers import open_session_in_studio

PHONE = {"width": 390, "height": 844}


def _status(base_url: str, sid: str) -> str | None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.get(f"/v1/sessions/{sid}")
    return r.json().get("status") if r.status_code == 200 else None


def _wait_status(base_url: str, sid: str, want: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = _status(base_url, sid)
        if last == want:
            return
        time.sleep(0.2)
    raise AssertionError(f"session {sid} never reached {want!r}; last status {last!r}")


@pytest.mark.timeout(180)
def test_park_and_end_are_in_the_overflow_and_the_palette_and_say_what_happened(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    _registry, mock_base_url = mock_llm_lan
    ids = seed_scripted_agent(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path, prefix="sact")
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = start_session(client, ids, "park me, then end me", auto_start=False)
    open_session_in_studio(page, console_url, ids["workspace"], sid)

    page.get_by_test_id("nv-session-overflow").click()
    expect(page.get_by_test_id("nv-session-park")).to_be_visible()
    expect(page.get_by_test_id("nv-session-end")).to_be_visible()
    page.get_by_test_id("nv-session-park").click()
    expect(page.locator(".toast", has_text="paused")).to_be_visible(timeout=10_000)
    _wait_status(base_url, sid, "paused")

    # A paused session has nothing left to park: the overflow stops offering it (it used to toast "paused" for a no-op).
    page.get_by_test_id("nv-session-overflow").click()
    expect(page.get_by_test_id("nv-session-end")).to_be_visible()
    expect(page.get_by_test_id("nv-session-park")).to_have_count(0, timeout=10_000)
    page.get_by_test_id("nv-session-overflow").click()

    # End is a palette verb too, and it asks first, naming the action on the button.
    run_verb(page, "End Session")
    confirm = page.get_by_test_id("dialog-confirm")
    expect(confirm).to_have_text("End session", timeout=10_000)
    confirm.click()
    expect(page.locator(".toast", has_text="Session ended")).to_be_visible(timeout=10_000)
    _wait_status(base_url, sid, "ended")


@pytest.mark.timeout(180)
def test_deleting_a_running_session_says_why_and_it_can_be_ended_then_deleted(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    registry, mock_base_url = mock_llm_lan
    ids = seed_scripted_agent(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path, prefix="sact")
    registry.register(ids["model_name"], [Rule(emit_text=" ".join(["word"] * 60), chunk_delay_s=1.0, text_chunk_words=1)])
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = start_session(client, ids, "a long turn", auto_start=True)
    _wait_status(base_url, sid, "running")
    open_session_in_studio(page, console_url, ids["workspace"], sid)

    page.get_by_test_id("nv-session-overflow").click()
    page.get_by_test_id("nv-session-delete").click()
    confirm = page.get_by_test_id("dialog-confirm")
    expect(confirm).to_have_text("Delete", timeout=10_000)
    confirm.click()
    error = page.locator(".toast.toast-error", has_text="Delete failed")
    expect(error).to_be_visible(timeout=10_000)
    assert _status(base_url, sid) == "running", "the 409 left the session alone"
    expect(page.get_by_test_id(f"nv-session-doc:{sid}")).to_be_visible()

    # Parking a RUNNING session only flags it (the worker pauses it at the turn boundary), and the toast says so.
    page.get_by_test_id("nv-session-overflow").click()
    page.get_by_test_id("nv-session-park").click()
    expect(page.locator(".toast", has_text="Pause requested")).to_be_visible(timeout=10_000)

    # The rail's right-click menu goes through the same path: it asks, then says what happened.
    row = session_row(page, sid, ids["workspace"])
    row.first.click(button="right")
    menu = page.get_by_test_id(f"nv-rail-session-menu:{sid}")
    expect(menu).to_be_visible(timeout=10_000)
    menu.get_by_text("End", exact=True).click()
    expect(page.get_by_test_id("dialog-confirm")).to_have_text("End session", timeout=10_000)
    page.get_by_test_id("dialog-confirm").click()
    expect(page.locator(".toast", has_text="Session ended")).to_be_visible(timeout=10_000)
    _wait_status(base_url, sid, "ended")

    page.get_by_test_id("nv-session-overflow").click()
    page.get_by_test_id("nv-session-delete").click()
    page.get_by_test_id("dialog-confirm").click()
    expect(page.locator(".toast", has_text="Session deleted")).to_be_visible(timeout=10_000)
    assert _status(base_url, sid) is None, "an ended session deletes"


@pytest.mark.timeout(180)
def test_a_phone_can_park_and_end_a_session_and_is_not_offered_split_right(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    _registry, mock_base_url = mock_llm_lan
    ids = seed_scripted_agent(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path, prefix="sact")
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = start_session(client, ids, "end me on a phone", auto_start=False)

    page.set_viewport_size(PHONE)
    page.goto(shell_url(console_url, ids["workspace"]) + f"?doc=session:{sid}")
    expect(page.get_by_test_id("nv-mob-session-screen")).to_be_visible(timeout=20_000)
    page.get_by_test_id("nv-session-overflow").click()
    expect(page.get_by_test_id("nv-session-park")).to_be_visible()
    expect(page.get_by_test_id("nv-session-end")).to_be_visible()
    expect(page.locator('[data-verb="session.splitRight"]')).to_have_count(0)   # a phone has one pane

    page.get_by_test_id("nv-session-end").click()
    page.get_by_test_id("dialog-confirm").click()
    expect(page.locator(".toast", has_text="Session ended")).to_be_visible(timeout=10_000)
    _wait_status(base_url, sid, "ended")
