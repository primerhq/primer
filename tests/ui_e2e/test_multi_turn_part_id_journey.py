"""01a04e02: a second turn's live text part must render, not be silently
dropped because it reused the first turn's already-finalized part_id.

primer/tap/delta.py's part_id(node_id, kind) had no turn_no at all - the
frontend's session-store.js keeps `store.parts` alive for the WHOLE
SESSION (scrollback needs a finished turn's parts to still render, not
just the live one), so turn 2's part_start landed on turn 1's entry
(SS_apply's part_start guard only creates when the key is ABSENT) and
then hit it already `final: true`, dropping every one of turn 2's
deltas. Store-level proof lives in tests/ui/test_session_store.py and
tests/session/test_persistence.py; this is the live, real-HTTP-round-
trip version the lead's brief explicitly asked for: two real turns
against the mock, second turn's answer must actually paint.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import httpx
from playwright.sync_api import expect

from tests._support.mock_llm import Rule
from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests.ui_e2e._studio_helpers import open_session_in_studio


def _seed(base_url: str, mock_base_url: str, suffix: str, tmp_path: Path) -> dict:
    ids = {
        "llm": f"mt-llm-{suffix}", "wp": f"mt-wp-{suffix}",
        "tpl": f"mt-tpl-{suffix}", "agent": f"mt-ag-{suffix}",
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": ids["llm"], "provider": "openchat",
            "models": [{"name": "scripted:multi-turn", "context_length": 8192}],
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
            "id": ids["tpl"], "description": "multi-turn part_id journey",
            "provider_id": ids["wp"], "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl failed: {r.status_code} {r.text}"

        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"seed workspace failed: {r.status_code} {r.text}"
        ids["workspace"] = r.json()["id"]

        r = c.post("/v1/agents", json={
            "id": ids["agent"], "description": "multi-turn part_id journey agent",
            "model": agent_model(ids["llm"], "scripted:multi-turn"),
            "tools": [],
        })
        assert r.status_code == 201, f"seed agent failed: {r.status_code} {r.text}"
    return ids


def _wait_for_session_state(
    client: httpx.Client, sid: str, target: str, *, timeout_s: float = 15.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        r = client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last.get("session_state") == target:
            return last
        time.sleep(0.1)
    raise AssertionError(
        f"never reached session_state={target!r}, last observed: {last}"
    )


def test_second_turns_answer_renders_not_silently_dropped(
    page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    registry, mock_base_url = mock_llm_lan
    registry.register("scripted:multi-turn", [
        Rule(
            when_last_user_contains="first-turn-instruction",
            emit_text="the first turn answer",
        ),
        Rule(
            when_last_user_contains="second-turn-instruction",
            emit_text="the second turn answer",
        ),
    ])
    suffix = uuid.uuid4().hex[:8]
    ids = _seed(base_url, mock_base_url, suffix, tmp_path)
    wid = ids["workspace"]

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        r = client.post(f"/v1/workspaces/{wid}/sessions", json={
            "binding": {"kind": "agent", "agent_id": ids["agent"]},
            "initial_instructions": "first-turn-instruction",
            "auto_start": True,
        })
        assert r.status_code == 201, f"create session failed: {r.status_code} {r.text}"
        sid = r.json()["id"]

        # Turn 1 completes. 01a0518a flipped _CLEAN_TURN_RESTS_PARKED on
        # by default: a clean stop now rests the session WAITING/parked
        # (session_state="parked") instead of ending it - see dispatch.
        # py's _post_turn_status docstring. Wait on session_state, not
        # the raw status field, matching what the flag's own e2e
        # (tests/e2e/test_agent_phase_sequence_e2e.py) checks.
        _wait_for_session_state(client, sid, "parked")

        # Sending a new message into a resting (parked) session resumes
        # it in place (wake_session's _RESUMABLE set already covers
        # WAITING) - this is turn 2, same session, same node, same part
        # kind, and (pre-fix) the exact same part_id as turn 1's.
        r = client.post(
            f"/v1/workspaces/{wid}/sessions/{sid}/steer",
            json={"instruction": "second-turn-instruction"},
        )
        assert r.status_code in (200, 201, 202), f"steer failed: {r.status_code} {r.text}"
        _wait_for_session_state(client, sid, "parked")

        open_session_in_studio(page, console_url, wid, sid)
        doc = page.get_by_test_id(f"nv-session-doc:{sid}")
        expect(doc).to_contain_text("the first turn answer", timeout=10_000)
        expect(doc).to_contain_text("the second turn answer", timeout=10_000)
