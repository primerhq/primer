"""Dogfood round 2: the chat header's context meter must serve an
HONEST denominator (docs: the lead's task 01a0533e). A discovery-seeded
fake 32000 used to flow all the way to a real user's model profile and
render as a confident-looking "98k / 32k" against an actual 131k model.

Covers the two rendering branches end to end, through a real HTTP round
trip against the container stack (the same shape 01a052a5's earlier
usage/context_length wiring needed real e2e coverage for, per
test_agent_phase_indicator_journey.py's own precedent):

- a profile whose context_length is the exact legacy seed (32000) must
  render usage ALONE, no denominator (primer/api/routers/sessions.py's
  _session_context_length treats that value as "never learned");
- a profile with a real value renders "usedk / ctxk" with the meter's
  data-pct reflecting the real fraction.
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


def _seed(base_url: str, mock_base_url: str, suffix: str, tmp_path: Path, *, context_length: int) -> dict:
    ids = {
        "llm": f"meter-llm-{suffix}", "wp": f"meter-wp-{suffix}",
        "tpl": f"meter-tpl-{suffix}", "agent": f"meter-ag-{suffix}",
    }
    model_name = f"scripted:meter-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": ids["llm"], "provider": "openchat",
            "models": [{"name": model_name, "context_length": context_length}],
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
            "id": ids["tpl"], "description": "context meter journey",
            "provider_id": ids["wp"], "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl failed: {r.status_code} {r.text}"

        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"seed workspace failed: {r.status_code} {r.text}"
        ids["workspace"] = r.json()["id"]

        r = c.post("/v1/agents", json={
            "id": ids["agent"], "description": "context meter journey agent",
            "model": agent_model(ids["llm"], model_name),
            "tools": [],
        })
        assert r.status_code == 201, f"seed agent failed: {r.status_code} {r.text}"
    ids["model_name"] = model_name
    return ids


def _wait_for_turn_to_settle(client: httpx.Client, sid: str, timeout_s: float = 20.0) -> dict:
    """Poll until the turn is no longer running - a simple completed
    turn resolves almost immediately server-side (no chunk delays in
    the scripted rule), but the row still needs a moment to reflect it.

    Overlay-flake hunt hardening: "not running" alone is ALSO true
    before the worker has dispatched the turn at all
    (poll_interval_seconds=1.0, scripts/e2e/bringup.sh's config) - a
    check-too-early race that's invisible on an idle box (dispatch
    beats the first 100ms poll) but real on a contended CI runner.
    Require "running" to have been observed at least once first.
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
def test_context_meter_null_and_real_denominator(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    registry, mock_base_url = mock_llm_lan
    suffix = uuid.uuid4().hex[:8]

    # Session A: the exact legacy-seeded fake - must render usage alone.
    fake_ids = _seed(base_url, mock_base_url, f"fake-{suffix}", tmp_path, context_length=32_000)
    registry.register(fake_ids["model_name"], [Rule(emit_text="All done here.")])

    # Session B: a real, honest window - must render "used / ctx".
    real_ids = _seed(base_url, mock_base_url, f"real-{suffix}", tmp_path, context_length=131_072)
    registry.register(real_ids["model_name"], [Rule(emit_text="All done here too.")])

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        r = client.post(f"/v1/workspaces/{fake_ids['workspace']}/sessions", json={
            "binding": {"kind": "agent", "agent_id": fake_ids["agent"]},
            "initial_instructions": "context-meter-journey: answer briefly",
            "auto_start": True,
        })
        assert r.status_code == 201, f"create session failed: {r.status_code} {r.text}"
        fake_sid = r.json()["id"]
        _wait_for_turn_to_settle(client, fake_sid)

        r = client.post(f"/v1/workspaces/{real_ids['workspace']}/sessions", json={
            "binding": {"kind": "agent", "agent_id": real_ids["agent"]},
            "initial_instructions": "context-meter-journey: answer briefly",
            "auto_start": True,
        })
        assert r.status_code == 201, f"create session failed: {r.status_code} {r.text}"
        real_sid = r.json()["id"]
        _wait_for_turn_to_settle(client, real_sid)

    # --- Session A: legacy-seeded fake -> usage alone, no denominator --------
    open_session_in_studio(page, console_url, fake_ids["workspace"], fake_sid)
    usage_label = page.get_by_test_id("nv-usage-label")
    expect(usage_label).not_to_have_text("", timeout=15_000)
    text = usage_label.inner_text()
    assert "/" not in text, (
        f"a legacy-seeded (unknown) context_length must render usage alone, "
        f"got {text!r}"
    )
    assert text.endswith("k"), f"expected a bare 'Nk' label, got {text!r}"
    assert page.get_by_test_id("nv-usage").get_attribute("data-pct") == "0", (
        "no known denominator means no bar fill either"
    )

    # --- Session B: real context_length -> "used / ctx" ----------------------
    page.reload()
    open_session_in_studio(page, console_url, real_ids["workspace"], real_sid)
    usage_label = page.get_by_test_id("nv-usage-label")
    expect(usage_label).to_contain_text("/", timeout=15_000)
    text = usage_label.inner_text()
    assert text.endswith("131k"), f"expected the real context window, got {text!r}"
    pct = int(page.get_by_test_id("nv-usage").get_attribute("data-pct"))
    assert 0 <= pct <= 100
