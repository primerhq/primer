"""Journey: a turn that failed can be sent again from its error card (console review C-024).

The model answers HTTP 500 and the loop's own retries run out. An interactive session now RESTS after that (C-024: ``waiting``, no ``ended_reason``,
``last_turn_error`` on the row) instead of ending as ``failed``; either way the console shows one error card and the line "Send a message to
try again". The card carries a Retry that sends the failed instruction again. The mock LLM is scripted to fail, and then (the test swaps the
script before it clicks) to answer, so the click is the only thing between the failed state and a working one. Nothing in the console is mocked.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.mock_llm import Rule
from tests._support.smk import smk
from tests.ui_e2e._studio_helpers import open_session_in_studio
from tests.ui_e2e.test_trace_sidebar_journey import _seed

pytestmark = smk("SMK-UI-06", status="partial")

INSTRUCTION = "retry-journey: summarise the plan"


def _failed_session(base_url: str, mock_llm_lan, tmp_path: Path) -> tuple[str, str, object, str]:
    registry, mock_base_url = mock_llm_lan
    ids = _seed(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path)
    wid = ids["workspace"]
    registry.register(ids["model_name"], [Rule(emit_status=500, emit_error_message="upstream exploded")])
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        r = client.post(f"/v1/workspaces/{wid}/sessions", json={
            "binding": {"kind": "agent", "agent_id": ids["agent"]}, "initial_instructions": INSTRUCTION, "auto_start": True,
        })
        assert r.status_code == 201, f"create session failed: {r.status_code} {r.text}"
        sid = r.json()["id"]
        deadline = time.monotonic() + 60
        row: dict = {}
        while time.monotonic() < deadline:
            row = client.get(f"/v1/sessions/{sid}").json()
            # the stamp is written BEFORE the status moves: wait for both, or a poll in between reads a failure exit still in flight
            if row.get("last_turn_error") and row.get("status") != "running":
                break
            time.sleep(0.5)
        assert row.get("status") == "waiting" and row.get("ended_reason") is None, row
        assert row["last_turn_error"]["code"] == "server_error", row
    return wid, sid, registry, ids["model_name"]


@pytest.mark.ui_e2e
@pytest.mark.timeout(150)
def test_retry_sends_the_failed_instruction_again_and_the_session_recovers(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
) -> None:
    wid, sid, registry, model = _failed_session(base_url, mock_llm_lan, tmp_path)
    open_session_in_studio(page, console_url, wid, sid)

    error_card = page.locator(".nv-turn-error")
    expect(error_card).to_have_count(1, timeout=20_000)
    retry = error_card.locator('[data-testid^="nv-turn-retry:"]')
    expect(retry).to_be_visible()

    registry.register(model, [Rule(emit_text="recovered after the retry")])
    retry.click()

    expect(page.get_by_text("recovered after the retry")).to_be_visible(timeout=30_000)
    expect(page.locator(".nv-turn-user").filter(has_text=INSTRUCTION)).to_have_count(2)
    expect(page.locator('[data-testid^="nv-turn-retry:"]')).to_have_count(0)
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        assert client.get(f"/v1/sessions/{sid}").json().get("session_state") != "ended"


@pytest.mark.ui_e2e
@pytest.mark.timeout(150)
def test_no_retry_is_offered_once_the_operator_has_sent_something_else(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
) -> None:
    wid, sid, registry, model = _failed_session(base_url, mock_llm_lan, tmp_path)
    open_session_in_studio(page, console_url, wid, sid)
    expect(page.locator('[data-testid^="nv-turn-retry:"]')).to_have_count(1, timeout=20_000)

    registry.register(model, [Rule(emit_text="a different answer")])
    composer = page.get_by_test_id("nv-composer-input")
    composer.fill("a different question")
    composer.press("Enter")

    expect(page.get_by_text("a different answer")).to_be_visible(timeout=30_000)
    expect(page.locator('[data-testid^="nv-turn-retry:"]')).to_have_count(0)
