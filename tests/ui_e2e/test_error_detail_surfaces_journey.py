"""Dogfood round 2 SEV add-on: the console's error toast/banner used to
render only the RFC7807 "title" ("Conflict (req-...)") even though the
backend already threads the real explanation through "detail"
(primer/api/errors.py). Fixed across every ApiError-consuming site in
ui/components/console/ (nv-session-doc.jsx and friends) to prefer
`err.detail`, falling back to the title only when detail is absent -
tests/ui/test_console_session_doc.py pins the source-level fix; this is
the live, real-HTTP-round-trip proof the lead's own report described:
force a 409 (a rewind on a busy session) and confirm the DETAIL text is
what actually renders, not the bare title.
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
        "llm": f"errdet-llm-{suffix}", "wp": f"errdet-wp-{suffix}",
        "tpl": f"errdet-tpl-{suffix}", "agent": f"errdet-ag-{suffix}",
    }
    model_name = f"scripted:errdet-{suffix}"
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
            "id": ids["tpl"], "description": "error detail journey",
            "provider_id": ids["wp"], "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl failed: {r.status_code} {r.text}"

        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"seed workspace failed: {r.status_code} {r.text}"
        ids["workspace"] = r.json()["id"]

        r = c.post("/v1/agents", json={
            "id": ids["agent"], "description": "error detail journey agent",
            "model": agent_model(ids["llm"], model_name),
            "tools": [],
        })
        assert r.status_code == 201, f"seed agent failed: {r.status_code} {r.text}"
    ids["model_name"] = model_name
    return ids


def _wait_for(client: httpx.Client, sid: str, *, state: str, timeout_s: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        r = client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last.get("session_state") == state:
            return last
        time.sleep(0.1)
    raise AssertionError(f"never reached session_state={state!r}, last observed: {last}")


@pytest.mark.timeout(90)
def test_rewind_409_shows_the_real_detail_not_the_bare_title(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    registry, mock_base_url = mock_llm_lan
    suffix = uuid.uuid4().hex[:8]
    ids = _seed(base_url, mock_base_url, suffix, tmp_path)
    wid = ids["workspace"]

    # First turn resolves immediately; the second is held "running" long
    # enough to attempt a rewind while a turn is genuinely in flight -
    # the backend's own 409 condition (workspaces.py's rewind_session:
    # "session is not idle; rewind requires no turn in flight").
    registry.register(ids["model_name"], [
        Rule(when_last_user_contains="first message", emit_text="ok"),
        Rule(
            when_last_user_contains="second message", emit_text="still working on it",
            chunk_delay_s=1.0, text_chunk_words=1,
        ),
    ])

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        r = client.post(f"/v1/workspaces/{wid}/sessions", json={
            "binding": {"kind": "agent", "agent_id": ids["agent"]},
            "initial_instructions": "error-detail-journey: first message",
            "auto_start": True,
        })
        assert r.status_code == 201, f"create session failed: {r.status_code} {r.text}"
        sid = r.json()["id"]
        _wait_for(client, sid, state="parked")

        r = client.post(f"/v1/workspaces/{wid}/sessions/{sid}/steer", json={
            "instruction": "error-detail-journey: second message, please keep busy",
        })
        assert r.status_code == 200, f"steer failed: {r.status_code} {r.text}"
        _wait_for(client, sid, state="running")

    open_session_in_studio(page, console_url, wid, sid)

    # The first user message is now rewind-eligible (a second, newer
    # user_input exists) - click its icon while the second turn is
    # still genuinely running.
    rewind_icon = page.locator('[data-testid^="nv-rewind-here:"]').first
    expect(rewind_icon).to_be_visible(timeout=15_000)
    rewind_icon.click()
    page.get_by_test_id("dialog-confirm").click()

    toasts = page.get_by_test_id("nv-toasts")
    expect(toasts).to_contain_text(
        "session is not idle; rewind requires no turn in flight", timeout=10_000,
    )
    # The bug being fixed: the bare RFC7807 title alone must not be all
    # that renders (a toast containing the real detail may still ALSO
    # carry "Conflict" as a prefix/title line - what must not happen is
    # the detail being silently dropped, which this assertion above
    # already proves by requiring the detail text to be present).
