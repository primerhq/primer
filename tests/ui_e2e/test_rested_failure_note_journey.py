"""Journey: a session that rests after a transport failure says why its last turn failed and what to do (console review C-024, the console part).

The model answers HTTP 500 and the loop's own retries run out; the interactive session RESTS (``waiting``, no ``ended_reason``, ``last_turn_error`` on the row) instead of ending. It has an
error card but no end divider and no end note, so the advice for THAT failure (``NV_failureWords``) was shown nowhere. The note under the transcript says it, and goes away once the
next turn starts (the server clears ``last_turn_error``). The mock LLM is scripted to fail and then, before the Retry click, to answer. Nothing in the console is mocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests._support.mock_llm import Rule
from tests._support.smk import smk
from tests.ui_e2e._studio_helpers import open_session_in_studio
from tests.ui_e2e.test_retry_failed_turn_journey import _failed_session

pytestmark = smk("SMK-UI-06", status="partial")


@pytest.mark.ui_e2e
@pytest.mark.timeout(150)
def test_a_resting_session_says_why_it_failed_and_the_note_goes_with_the_next_turn(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
) -> None:
    wid, sid, registry, model = _failed_session(base_url, mock_llm_lan, tmp_path)
    open_session_in_studio(page, console_url, wid, sid)

    note = page.get_by_test_id("nv-rested-note")
    expect(note).to_be_visible(timeout=20_000)
    expect(note).to_contain_text("The last turn failed: the model provider had a server error.")
    expect(note).to_contain_text("Send a message to try again in a moment")
    expect(page.get_by_test_id("nv-ended-note")).to_have_count(0)   # the session did not end: no end divider, no end note

    registry.register(model, [Rule(emit_text="recovered after the retry")])
    page.locator('.nv-turn-error [data-testid^="nv-turn-retry:"]').click()
    expect(page.get_by_text("recovered after the retry")).to_be_visible(timeout=30_000)
    expect(note).to_have_count(0, timeout=10_000)
