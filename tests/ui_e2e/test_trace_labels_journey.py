"""Journey: the trace pane names its badges and shows what a model call cost (console review C-017).

Rows read ``T 12:32 AM workspace__ls 0s`` and ``A 12:32 AM operator 1s``: single-letter badges with no legend, and no token counts although the timeline node carries them.
A real tool-calling turn is scripted through the mock LLM (the shape of ``test_trace_sidebar_journey``), the trace is opened off the finished turn, and both surfaces, the sidebar's
one-liners and the maximize overlay's rows, are read in the real DOM.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from tests._support.mock_llm import Rule
from tests._support.smk import smk
from tests.ui_e2e._studio_helpers import open_session_in_studio
from tests.ui_e2e.test_trace_sidebar_journey import _seed, _wait_for_turn_to_settle

pytestmark = smk("SMK-UI-06", status="partial")


def _open_trace(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path) -> None:
    registry, mock_base_url = mock_llm_lan
    ids = _seed(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path)
    wid = ids["workspace"]
    registry.register(ids["model_name"], [
        Rule(when_tool_result=False, emit_tool="misc__uuid_v4", emit_args={}),
        Rule(when_tool_result=True, emit_text="Based on the generated id, done."),
    ])
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        r = client.post(f"/v1/workspaces/{wid}/sessions", json={
            "binding": {"kind": "agent", "agent_id": ids["agent"]}, "initial_instructions": "trace-labels: use the tool then answer", "auto_start": True,
        })
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        _wait_for_turn_to_settle(client, sid)
    open_session_in_studio(page, console_url, wid, sid)
    page.locator('[data-testid^="nv-trace-open:"]').first.click()
    expect(page.get_by_test_id("nv-trace-split")).to_be_visible(timeout=10_000)


def _assert_named_badges_and_tokens(scope) -> None:
    expect(scope.locator('.nv-trace-glyph[title="Tool call"]').first).to_be_visible(timeout=10_000)
    model_badge = scope.locator('.nv-trace-glyph[title="Model call"]').first
    expect(model_badge).to_be_visible()
    expect(model_badge).to_have_attribute("aria-label", "Model call")
    tokens = scope.locator(".nv-trace-tokens").first
    expect(tokens).to_be_visible()
    expect(tokens).to_contain_text("in")
    expect(tokens).to_contain_text("out")
    assert scope.locator('.nv-trace-line:has(.nv-trace-glyph[title="Tool call"]) .nv-trace-tokens').count() == 0, "a tool call has no tokens"


def test_the_sidebar_rows_name_their_badges_and_show_a_model_calls_tokens(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path) -> None:
    _open_trace(page, base_url, console_url, mock_llm_lan, tmp_path)
    _assert_named_badges_and_tokens(page.get_by_test_id("nv-trace-split"))


def test_the_maximize_overlay_rows_do_too(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path) -> None:
    _open_trace(page, base_url, console_url, mock_llm_lan, tmp_path)
    page.get_by_test_id("nv-trace-split").get_by_test_id("nv-trace-maximize-open").click()
    overlay = page.get_by_test_id("nv-trace-maximize")
    expect(overlay).to_be_visible(timeout=10_000)
    _assert_named_badges_and_tokens(overlay)
