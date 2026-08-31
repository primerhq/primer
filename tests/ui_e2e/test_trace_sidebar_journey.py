"""Dogfood round 2: the trace sidebar redesign (the lead's task
01a0533e) - one-line "[T]/[A]" rows in the persistent sidebar, and a
maximize overlay whose own rows expand to show BOTH arguments AND the
paired result (primer/session/timeline.py's tool_result branch now
attaches a size-capped result alongside status/duration_ms).

Real HTTP round trip against the container stack, same shape as
test_agent_phase_indicator_journey.py / test_context_meter_journey.py -
a tool-calling turn is scripted so the trace has at least one [T] (tool_call)
and [A] (llm_call) entry, and a real result to expand.
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
    ids = {
        "llm": f"trace-llm-{suffix}", "wp": f"trace-wp-{suffix}",
        "tpl": f"trace-tpl-{suffix}", "agent": f"trace-ag-{suffix}",
    }
    model_name = f"scripted:trace-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": ids["llm"], "provider": "openchat",
            "models": [{"name": model_name, "context_length": 131_072}],
            "config": {"url": mock_base_url, "flavor": "other"},
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed llm failed: {r.status_code} {r.text}"

        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"], "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed wp failed: {r.status_code} {r.text}"

        r = c.post("/v1/workspace_templates", json={
            "id": ids["tpl"], "description": "trace sidebar journey",
            "provider_id": ids["wp"], "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl failed: {r.status_code} {r.text}"

        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"seed workspace failed: {r.status_code} {r.text}"
        ids["workspace"] = r.json()["id"]

        r = c.post("/v1/agents", json={
            "id": ids["agent"], "description": "trace sidebar journey agent",
            "model": agent_model(ids["llm"], model_name),
            "tools": ["misc__uuid_v4"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.status_code} {r.text}"
    ids["model_name"] = model_name
    return ids


def _wait_for_turn_to_settle(client: httpx.Client, sid: str, timeout_s: float = 20.0) -> dict:
    """Wait for the auto-started turn to finish, not just for a poll to
    observe "not running" - those are the SAME reading before the
    worker has dispatched the turn at all (poll_interval_seconds=1.0,
    scripts/e2e/bringup.sh's config) and after it has finished, and a
    check-too-early race here (returning on the pre-dispatch reading)
    is invisible on an idle box - dispatch beats the first 100ms poll -
    but real on a contended CI runner, where this returned before the
    tool call even started and let the test open a still-mid-stream
    session (the overlay-flake hunt's specimen on this file: two CI
    failures, GHA run 33426311517, screenshot showed "forming a tool
    call... STREAMING 10s" under an already-opened trace overlay).
    """
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    seen_running = False
    while time.monotonic() < deadline:
        r = client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last.get("session_state") == "running":
            seen_running = True
        elif seen_running:
            return last
        time.sleep(0.1)
    raise AssertionError(f"turn never settled, last observed: {last}")


@pytest.mark.timeout(90)
def test_trace_rows_are_one_line_and_maximize_overlay_expands(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    registry, mock_base_url = mock_llm_lan
    suffix = uuid.uuid4().hex[:8]
    ids = _seed(base_url, mock_base_url, suffix, tmp_path)
    wid = ids["workspace"]

    registry.register(ids["model_name"], [
        Rule(when_tool_result=False, emit_tool="misc__uuid_v4", emit_args={}),
        Rule(when_tool_result=True, emit_text="Based on the generated id, done."),
    ])

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        r = client.post(f"/v1/workspaces/{wid}/sessions", json={
            "binding": {"kind": "agent", "agent_id": ids["agent"]},
            "initial_instructions": "trace-sidebar-journey: use the tool then answer",
            "auto_start": True,
        })
        assert r.status_code == 201, f"create session failed: {r.status_code} {r.text}"
        sid = r.json()["id"]
        _wait_for_turn_to_settle(client, sid)

    open_session_in_studio(page, console_url, wid, sid)

    # --- Open the trace sidebar off the completed turn's own affordance ------
    page.locator('[data-testid^="nv-trace-open:"]').first.click()
    sidebar = page.get_by_test_id("nv-trace-split")
    expect(sidebar).to_be_visible(timeout=10_000)

    # --- Sidebar rows: one line each, never expandable ------------------------
    lines = sidebar.locator('[data-testid^="nv-trace-line:"]')
    expect(lines.first).to_be_visible(timeout=10_000)
    tag_names = [
        lines.nth(i).evaluate("el => el.tagName")
        for i in range(lines.count())
    ]
    assert all(t == "DIV" for t in tag_names), (
        f"every sidebar row must be a plain div, not a button: {tag_names}"
    )
    glyphs = sidebar.locator(".nv-trace-glyph").all_inner_texts()
    assert "T" in glyphs, "the tool_call row must show the [T] glyph"
    assert "A" in glyphs, "the llm_call rows must show the [A] glyph"
    # Clicking a sidebar row must do nothing - no inline detail appears.
    lines.first.click()
    expect(sidebar.locator(".nv-trace-args")).to_have_count(0)

    # --- Maximize opens the overlay --------------------------------------------
    sidebar.get_by_test_id("nv-trace-maximize-open").click()
    overlay = page.get_by_test_id("nv-trace-maximize")
    expect(overlay).to_be_visible(timeout=10_000)

    # --- Expanding the tool_call row shows BOTH arguments and the result ------
    tool_toggle = overlay.locator('[data-testid^="nv-trace-row-toggle:"]').filter(
        has=page.locator('.nv-trace-glyph[data-kind="tool"]')
    ).first
    expect(tool_toggle).to_be_visible(timeout=10_000)
    tool_toggle.click()
    detail = overlay.locator(".nv-trace-detail").first
    expect(detail).to_be_visible(timeout=10_000)
    # .nv-trace-detail-sec is CSS text-transform: uppercase - inner_text
    # returns the rendered (uppercased) text, not the lowercase JSX literal.
    detail_text = detail.inner_text().lower()
    assert "arguments" in detail_text
    assert "result" in detail_text
    assert "no result yet" not in detail_text, (
        "the tool_result record landed before the turn completed - the "
        "overlay must show the real result, not the still-running placeholder"
    )

    # --- The overlay close button works ----------------------------------------
    overlay.get_by_test_id("nv-trace-maximize-close").click()
    expect(overlay).to_have_count(0)
