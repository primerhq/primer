"""Journey: on a phone, a RUNNING turn and a PENDING question have tap-sized controls too (console review C-035, the part a finished turn does not show).

``test_mobile_tap_targets_journey`` measures a finished turn. The rest of C-035 lives in two other states, each scripted through the mock LLM on a real session at 390x844:

* a turn that is still running (a slow stream): the session header's binding chip was 139x27, the status strip's inline "interrupt" button 75x22, and the mid-run send button
  said "+Q", which tells a reader nothing; it says "Queue" now;
* a turn parked on a question (``system__ask_user``): the card's "Answer & resume" button was 127x31.

Each control is read by its test id, from its bounding box (44px is the stylesheet's own mobile floor, ``--tap-min``). Nothing in the console is mocked.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.mock_llm import Rule
from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import shell_url

pytestmark = smk("SMK-UI-06", status="partial")

PHONE = {"width": 390, "height": 844}
TAP_MIN = 44


def _seed(base_url: str, mock_base_url: str, tmp_path: Path) -> dict:
    suffix = uuid.uuid4().hex[:8]
    model = f"scripted:states-{suffix}"
    ids = {"llm": f"st-llm-{suffix}", "wp": f"st-wp-{suffix}", "tpl": f"st-tpl-{suffix}", "agent": f"st-ag-{suffix}", "model": model}
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": ids["llm"], "provider": "openchat", "models": [{"name": model, "context_length": 131_072}],
            "config": {"url": mock_base_url, "flavor": "other"}, "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, r.text
        r = c.post("/v1/workspace_providers", json={"id": ids["wp"], "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path)}})
        assert r.status_code == 201, r.text
        r = c.post("/v1/workspace_templates", json={"id": ids["tpl"], "description": "states", "provider_id": ids["wp"], "backend": {"kind": "local"}})
        assert r.status_code == 201, r.text
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, r.text
        ids["workspace"] = r.json()["id"]
        r = c.post("/v1/agents", json={
            "id": ids["agent"], "description": "states", "model": agent_model(ids["llm"], model), "tools": ["system__ask_user"],
        })
        assert r.status_code == 201, r.text
    return ids


def _start(base_url: str, ids: dict, instruction: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(f"/v1/workspaces/{ids['workspace']}/sessions", json={
            "binding": {"kind": "agent", "agent_id": ids["agent"]}, "initial_instructions": instruction, "auto_start": True,
        })
        assert r.status_code == 201, r.text
        return r.json()["id"]


def _size(page: Page, testid: str) -> tuple[float, float]:
    box = page.get_by_test_id(testid).bounding_box()
    assert box, f"{testid} is not on the screen"
    return box["width"], box["height"]


@pytest.mark.ui_e2e
@pytest.mark.timeout(150)
def test_a_running_turns_controls_are_tap_sized_and_the_queue_button_says_queue(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
) -> None:
    registry, mock_base_url = mock_llm_lan
    ids = _seed(base_url, mock_base_url, tmp_path)
    registry.register(ids["model"], [Rule(emit_text=" ".join(["slow"] * 60), text_chunk_words=1, chunk_delay_s=0.5)])
    sid = _start(base_url, ids, "running-state: stream slowly")
    page.set_viewport_size(PHONE)
    page.goto(f"{shell_url(console_url, ids['workspace'])}?doc=session:{sid}")
    expect(page.get_by_test_id("nv-interrupt")).to_be_visible(timeout=30_000)

    small = {t: _size(page, t) for t in ("nv-binding-chip", "nv-interrupt", "nv-send") if min(_size(page, t)) < TAP_MIN}
    assert not small, f"controls under {TAP_MIN}px while a turn runs: {small}"
    send = page.get_by_test_id("nv-send")
    expect(send).to_have_attribute("data-mode", "queue")
    expect(send).to_have_text("Queue")


@pytest.mark.ui_e2e
@pytest.mark.timeout(150)
def test_a_pending_questions_answer_button_is_tap_sized(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
) -> None:
    registry, mock_base_url = mock_llm_lan
    ids = _seed(base_url, mock_base_url, tmp_path)
    registry.register(ids["model"], [Rule(emit_tool="system__ask_user", emit_args={"prompt": "Which environment should I deploy to?"})])
    sid = _start(base_url, ids, "ask-state: ask me something")
    page.set_viewport_size(PHONE)
    page.goto(f"{shell_url(console_url, ids['workspace'])}?doc=session:{sid}")
    expect(page.locator('[data-testid^="nv-ask:"]')).to_be_visible(timeout=40_000)

    width, height = _size(page, "nv-ask-submit")
    assert min(width, height) >= TAP_MIN, f"the answer button is {width}x{height}"
