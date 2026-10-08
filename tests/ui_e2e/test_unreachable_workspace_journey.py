"""Journey: a session whose workspace cannot be read says so instead of showing an empty conversation (A-08).

The messages read answers a typed 503 (``/errors/workspace-unreachable``) when the workspace's runtime does not answer; it used to
answer 200 with an empty list, byte-identical to a session that had written nothing, so the console drew an empty thread and the
history looked gone. The session row is real; only the messages read is answered with the 503 the server now gives.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests.ui_e2e._studio_helpers import open_session_in_studio


def _seed_session(base_url: str, mock_base_url: str, tmp_path: Path) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    ids = {"llm": f"un-llm-{suffix}", "wp": f"un-wp-{suffix}", "tpl": f"un-tpl-{suffix}", "agent": f"un-ag-{suffix}"}
    model_name = f"scripted:un-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": ids["llm"], "provider": "openchat", "models": [{"name": model_name, "context_length": 131_072}],
            "config": {"url": mock_base_url, "flavor": "other"}, "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, r.text
        assert c.post("/v1/workspace_providers", json={
            "id": ids["wp"], "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path)},
        }).status_code == 201
        assert c.post("/v1/workspace_templates", json={
            "id": ids["tpl"], "description": "unreachable journey", "provider_id": ids["wp"], "backend": {"kind": "local"},
        }).status_code == 201
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, r.text
        wid = r.json()["id"]
        assert c.post("/v1/agents", json={
            "id": ids["agent"], "description": "unreachable journey agent",
            "model": agent_model(ids["llm"], model_name), "tools": [],
        }).status_code == 201
        r = c.post(f"/v1/workspaces/{wid}/sessions", json={
            "binding": {"kind": "agent", "agent_id": ids["agent"]}, "initial_instructions": "hello", "auto_start": False,
        })
        assert r.status_code == 201, r.text
        return wid, r.json()["id"]


_PROBLEM = {
    "type": "/errors/workspace-unreachable", "title": "Workspace Unreachable", "status": 503,
    "detail": "The workspace that holds this session's log could not be reached.",
}


@pytest.mark.ui_e2e
@pytest.mark.timeout(90)
def test_an_unreadable_workspace_is_said_so_and_recovers_when_it_answers(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    _, mock_base_url = mock_llm_lan
    wid, sid = _seed_session(base_url, mock_base_url, tmp_path)
    down = {"on": True}

    def messages(route) -> None:
        if down["on"]:
            route.fulfill(status=503, content_type="application/problem+json", body=json.dumps(_PROBLEM))
        else:
            route.continue_()

    page.route(f"**/v1/sessions/{sid}/messages*", messages)
    open_session_in_studio(page, console_url, wid, sid)

    banner = page.get_by_test_id("nv-history-problem")
    expect(banner).to_be_visible(timeout=15_000)
    expect(banner).to_contain_text("unreachable")
    expect(banner).to_contain_text("not lost")

    down["on"] = False                       # the runtime comes back
    banner.get_by_role("button", name="Try again").click()
    expect(banner).to_have_count(0, timeout=15_000)
