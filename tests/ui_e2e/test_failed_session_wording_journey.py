"""Journey: a failed turn reads as ONE card in words, the ended session says why and what to do, and a session with no usage has no
empty meter (lead sweep 2026-10-08, A2 to A4).

Before: a model call that failed produced two red cards ("OpenAI network failure: APIConnectionError", then a bare "turn failed"
from the terminal marker), the end divider read only "session ended . failed", and a session with no usage drew an unlabelled empty
grey bar in its header. A scripted upstream 500 through the real OpenChatLLM client and the real dispatch makes the first two; a
session that was never started makes the third.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.mock_llm import Rule
from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests.ui_e2e._studio_helpers import open_session_in_studio


def _seed(base_url: str, mock_base_url: str, suffix: str, tmp_path: Path) -> dict:
    ids = {"llm": f"fw-llm-{suffix}", "wp": f"fw-wp-{suffix}", "tpl": f"fw-tpl-{suffix}", "agent": f"fw-ag-{suffix}"}
    model_name = f"scripted:fw-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": ids["llm"], "provider": "openchat",
            "models": [{"name": model_name, "context_length": 131_072}],
            "config": {"url": mock_base_url, "flavor": "other"},
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed llm failed: {r.status_code} {r.text}"
        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"], "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed wp failed: {r.status_code} {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": ids["tpl"], "description": "failed session wording journey",
            "provider_id": ids["wp"], "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl failed: {r.status_code} {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"seed workspace failed: {r.status_code} {r.text}"
        ids["workspace"] = r.json()["id"]
        r = c.post("/v1/agents", json={
            "id": ids["agent"], "description": "failed session wording journey agent",
            "model": agent_model(ids["llm"], model_name), "tools": [],
        })
        assert r.status_code == 201, f"seed agent failed: {r.status_code} {r.text}"
    ids["model_name"] = model_name
    return ids


def _start(client: httpx.Client, ids: dict, instructions: str, *, auto_start: bool) -> str:
    r = client.post(f"/v1/workspaces/{ids['workspace']}/sessions", json={
        "binding": {"kind": "agent", "agent_id": ids["agent"]}, "initial_instructions": instructions, "auto_start": auto_start,
    })
    assert r.status_code == 201, f"create session failed: {r.status_code} {r.text}"
    return r.json()["id"]


def _wait_until_ended(client: httpx.Client, sid: str, timeout_s: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get(f"/v1/sessions/{sid}").json()
        if last.get("status") == "ended":
            return last
        time.sleep(0.2)
    raise AssertionError(f"the session never ended, last observed: {last}")


def _wait_until_failed(client: httpx.Client, sid: str, timeout_s: float = 30.0) -> dict:
    """The turn failed. An upstream 5xx of an interactive session leaves it RESTING with ``last_turn_error`` set (C-024); other failures end it."""
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get(f"/v1/sessions/{sid}").json()
        if last.get("status") == "ended" or last.get("last_turn_error"):
            return last
        time.sleep(0.2)
    raise AssertionError(f"the turn never failed, last observed: {last}")


@pytest.mark.timeout(120)
def test_a_failed_upstream_turn_is_one_card_in_words_and_the_session_rests(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    registry, mock_base_url = mock_llm_lan
    ids = _seed(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path)
    registry.register(ids["model_name"], [Rule(emit_status=500, emit_error_message="scripted upstream failure")])

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        failed = _start(client, ids, "this will fail upstream", auto_start=True)
        row = _wait_until_failed(client, failed)
        unstarted = _start(client, ids, "never started", auto_start=False)

    # An upstream 500 is a transport failure: the session rests instead of ending (C-024), and the row still says the turn failed.
    assert row["status"] == "waiting" and row["ended_reason"] is None, row
    assert row["last_turn_error"]["code"] == "server_error", row

    open_session_in_studio(page, console_url, ids["workspace"], failed)
    cards = page.locator(".nv-turn-error")
    expect(cards).to_have_count(1, timeout=15_000)
    expect(cards.first).to_contain_text("The model provider had a server error.")
    expect(page.get_by_test_id("nv-ended-note")).to_have_count(0)   # the session did not end, so it has no end divider and no ended note

    # A session that was never started has no usage: no meter at all, not an unlabelled empty bar.
    open_session_in_studio(page, console_url, ids["workspace"], unstarted)
    expect(page.get_by_test_id("nv-session-head")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("nv-usage")).to_have_count(0)


@pytest.mark.timeout(120)
def test_a_model_that_dies_mid_answer_is_one_card_in_words_with_the_cause_below_it(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    """The common failure: the answer has started streaming and the provider dies. The stream's own Error row and dispatch's
    ERROR record (same words), and the bare terminal marker, used to be three red cards, and the cause read as the provider's raw
    text. The first journey above fails the request BEFORE a stream opens, which never produces the stream's Error row."""
    registry, mock_base_url = mock_llm_lan
    ids = _seed(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path)
    cause = "scripted upstream melted down"
    registry.register(ids["model_name"], [Rule(emit_text="Here is the start of an answer", fail_mid_stream=cause)])

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        failed = _start(client, ids, "this will die mid-answer", auto_start=True)
        _wait_until_ended(client, failed)

    open_session_in_studio(page, console_url, ids["workspace"], failed)
    cards = page.locator(".nv-turn-error")
    expect(cards).to_have_count(1, timeout=15_000)
    expect(cards.first).to_contain_text("The model stopped answering part-way through.")
    expect(page.locator(".nv-turn-error-detail")).to_contain_text(cause)
    note = page.get_by_test_id("nv-ended-note")
    expect(note).to_be_visible(timeout=10_000)
    expect(note).to_contain_text("the model call failed")
    expect(note).not_to_contain_text("llm_stream_error")
