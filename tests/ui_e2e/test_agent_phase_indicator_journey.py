"""Phase 2 (01a04ddf): the in-chat presence indicator + session_state
chip, driven by agent_phase/session_state, must survive a hard refresh at
every phase - the acceptance invariant (docs/superpowers/2026-08-29-
execution-lifecycle-vetting-and-revamp.md), applied to the RENDERED page
rather than the REST response (tests/e2e/test_agent_phase_sequence_e2e.py
already proves the served fields themselves survive a cold client; this
proves the CONSOLE actually paints them the same way pre/post refresh).

Uses the real slow-stream mock (tests/_support/mock_llm.py's
slow_turn_with_mid_stream_tool_call) over a real HTTP round-trip against
the container stack - the same shape 01a04d64-b4ba's live diagnosis and
01a04d92's SEV-2 fix needed and verified manually before this test
existed.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.mock_llm import slow_turn_with_mid_stream_tool_call
from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests.ui_e2e._studio_helpers import open_session_in_studio

# Generous relative to the backend e2e's 1-1.4s scenario: real page loads
# and reloads add real wall-clock the pure-REST test never pays, so each
# phase window needs enough margin that a slow CI runner still catches it
# before the next transition.
_TOTAL_SECONDS = 16.0


def _seed(base_url: str, mock_base_url: str, suffix: str, tmp_path: Path) -> dict:
    ids = {
        "llm": f"phase-llm-{suffix}", "wp": f"phase-wp-{suffix}",
        "tpl": f"phase-tpl-{suffix}", "agent": f"phase-ag-{suffix}",
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": ids["llm"], "provider": "openchat",
            "models": [{"name": "scripted:phase-indicator", "context_length": 8192}],
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
            "id": ids["tpl"], "description": "phase indicator journey",
            "provider_id": ids["wp"], "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl failed: {r.status_code} {r.text}"

        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"seed workspace failed: {r.status_code} {r.text}"
        ids["workspace"] = r.json()["id"]

        r = c.post("/v1/agents", json={
            "id": ids["agent"], "description": "phase indicator journey agent",
            "model": agent_model(ids["llm"], "scripted:phase-indicator"),
            "tools": ["misc__uuid_v4"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.status_code} {r.text}"
    return ids


def _wait_for_phase(
    client: httpx.Client, sid: str, *, session_state: str, agent_phase: str | None,
    timeout_s: float = 15.0,
) -> dict:
    """Poll the REST row (the same one the console itself polls) until it
    reaches the target (session_state, agent_phase) pair. Driving the UI
    checks off this signal, rather than a blind sleep, is what makes the
    test land inside each real, but short, phase window reliably."""
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        r = client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last.get("session_state") == session_state and (
            agent_phase is None or last.get("agent_phase") == agent_phase
        ):
            return last
        time.sleep(0.1)
    raise AssertionError(
        f"never reached session_state={session_state!r} agent_phase={agent_phase!r}, "
        f"last observed: {last}"
    )


def _expect_chip_state(page: Page, expected: str) -> None:
    """Retrying assertion: the FIRST paint after a mount/reload runs
    before the session poll resolves (NV_SessionStateChip defaults to
    "waiting" with no session yet - a real, brief loading state, not a
    served-truth violation of the acceptance invariant, which is about
    what renders once state has loaded, not zero-latency rendering)."""
    expect(page.get_by_test_id("nv-session-state-chip")).to_have_attribute(
        "data-state", expected, timeout=10_000,
    )


def _expect_indicator_phase(page: Page, expected: str) -> None:
    expect(page.get_by_test_id("nv-phase-indicator")).to_have_attribute(
        "data-phase", expected, timeout=10_000,
    )


@pytest.mark.timeout(120)
def test_phase_indicator_and_state_chip_survive_refresh_at_every_phase(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    registry, mock_base_url = mock_llm_lan
    registry.register(
        "scripted:phase-indicator",
        slow_turn_with_mid_stream_tool_call(
            tool_name="misc__uuid_v4", total_seconds=_TOTAL_SECONDS,
        ),
    )
    suffix = uuid.uuid4().hex[:8]
    ids = _seed(base_url, mock_base_url, suffix, tmp_path)
    wid = ids["workspace"]

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        r = client.post(f"/v1/workspaces/{wid}/sessions", json={
            "binding": {"kind": "agent", "agent_id": ids["agent"]},
            "initial_instructions": "phase-indicator-journey: use the tool then answer",
            "auto_start": True,
        })
        assert r.status_code == 201, f"create session failed: {r.status_code} {r.text}"
        sid = r.json()["id"]

        open_session_in_studio(page, console_url, wid, sid)

        # --- Phase 1: running + thinking (pre-tool-call) -------------------
        _wait_for_phase(client, sid, session_state="running", agent_phase="thinking")
        _expect_chip_state(page, "running")
        _expect_indicator_phase(page, "thinking")

        page.reload()
        open_session_in_studio(page, console_url, wid, sid)
        # The first paint after a reload runs before the session poll
        # resolves - a brief, expected loading state (see
        # _expect_chip_state's own comment), not a served-truth
        # violation, so this retries rather than reading instantly.
        _expect_chip_state(page, "running")
        # A fast tool call may have already advanced the server past
        # "thinking" by the time the reload completes - real time keeps
        # moving during a hard refresh's own round trip. What must be
        # true is that the CLIENT's post-refresh view matches whatever
        # the server reports RIGHT NOW, not the pre-refresh snapshot -
        # the acceptance invariant is "rebuilt from served state", not
        # "frozen in time".
        cur = client.get(f"/v1/sessions/{sid}").json()
        if cur.get("agent_phase") == "thinking":
            _expect_indicator_phase(page, "thinking")

        # --- Phase 2: running + executing (tool call in flight) -----------
        _wait_for_phase(client, sid, session_state="running", agent_phase="executing")
        _expect_indicator_phase(page, "executing")

        page.reload()
        open_session_in_studio(page, console_url, wid, sid)
        _expect_chip_state(page, "running")
        cur = client.get(f"/v1/sessions/{sid}").json()
        if cur.get("agent_phase") == "executing":
            _expect_indicator_phase(page, "executing")

        # --- Phase 3: running + responding (final answer streaming) -------
        _wait_for_phase(client, sid, session_state="running", agent_phase="responding")
        # The indicator is REPLACED by streaming text at this phase - it
        # must not still be showing a generic "Thinking"/"Executing" row
        # once real content is flowing.
        expect(page.get_by_test_id("nv-phase-indicator")).not_to_be_visible()
        expect(page.get_by_test_id(f"nv-session-doc:{sid}")).to_contain_text(
            "Based on what", timeout=10_000,
        )

        page.reload()
        open_session_in_studio(page, console_url, wid, sid)
        _expect_chip_state(page, "running")
        # NOT the exact prefix this time: the tap streams forward only
        # (no replay), so a client that reconnects mid-response starts
        # its live part from whatever chunk arrives FIRST after
        # reconnect, not from the true beginning - the reload's own
        # round-trip time means "Based on what" (the answer's first
        # words, already streamed and rendered PRE-refresh) can legitimately
        # be gone from the POST-refresh view. What the fix (session-
        # store.js's SS_apply text_delta branch, Phase 2 01a04ddf)
        # guarantees is that SOME of the answer renders, not nothing -
        # any fragment spanning the text proves that.
        expect(page.get_by_test_id(f"nv-session-doc:{sid}")).to_contain_text(
            re.compile(
                r"Based on what|tool returned|detailed answer|step by step|easy to follow"
            ),
            timeout=10_000,
        )
        expect(page.get_by_test_id("nv-phase-indicator")).not_to_be_visible()

        # --- Phase 4: parked (01a0518a flipped _CLEAN_TURN_RESTS_PARKED
        # on by default - a clean stop now correctly rests the session
        # session_state="parked" instead of ending it; agent_phase still
        # clears to None at rest either way, per dispatch.py's
        # _post_turn_status). ----
        _wait_for_phase(client, sid, session_state="parked", agent_phase=None)
        _expect_chip_state(page, "parked")

        page.reload()
        open_session_in_studio(page, console_url, wid, sid)
        _expect_chip_state(page, "parked")
        expect(page.get_by_test_id("nv-phase-indicator")).not_to_be_visible()
