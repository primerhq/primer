"""An admin agent points web fetch at an ordered list through the system tool (platform revamp 04, Task 2).

A scripted agent, run by the e2e user (the first registered user, so an admin), calls ``system__set_web_fetch_active_config`` once.
The REST singleton then reads back the list it sent: the tool and ``PUT /v1/web_fetch_active_config`` write the same row, and the
tool's admin-only role lets an admin's run through the tool manager's role floor. The test restores the built-in single ``local``
config and deletes the fetcher it made, whatever happens.
"""

from __future__ import annotations

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_completed,
)

pytestmark = pytest.mark.asyncio

_SET_TOOL = "system__set_web_fetch_active_config"


async def test_an_admin_agent_points_web_fetch_at_an_ordered_list(authed_client, mock_llm, unique_suffix, tmp_path) -> None:
    registry, base_url = mock_llm
    jina = f"jina-{unique_suffix}"
    scenario = f"scripted:web-fetch-config-{unique_suffix}"
    wanted = {"mode": "aggregated", "provider_ids": [jina, "local"]}

    created = await authed_client.post(
        "/v1/web_fetch_providers", json={"id": jina, "provider_type": "jina", "config": {"type": "jina"}},
    )
    assert created.status_code in (200, 201), created.text
    try:
        agent = await make_scripted_agent(
            authed_client, registry, base_url,
            suffix=f"wfc{unique_suffix}",
            scenario=scenario,
            tools=[_SET_TOOL],
            rules=[
                Rule(when_tool_result=False, emit_tool=_SET_TOOL, emit_args={"config": wanted}),
                Rule(when_tool_result=True, emit_text="done"),
            ],
        )
        wid = await make_local_workspace(authed_client, suffix=f"wfc{unique_suffix}", root=tmp_path)
        sid = await start_agent_session(
            authed_client, workspace_id=wid, agent_id=agent["agent_id"],
            instructions="Fetch pages with the Jina fetcher first and fall back to the local fetcher.",
        )
        final = await wait_completed(authed_client, sid, timeout_s=60)
        assert final.get("session_state") == "parked", final

        # What the tool answered, for the failure message: an access denial or a refusal shows up here.
        answers = [
            message.get("content")
            for request in registry.requests if request.get("model") == scenario
            for message in request.get("messages", []) if message.get("role") == "tool"
        ]
        active = await authed_client.get("/v1/web_fetch_active_config")
        assert active.status_code == 200, active.text
        assert active.json()["config"] == wanted, f"the tool answered: {answers}"
    finally:
        await authed_client.put("/v1/web_fetch_active_config", json={"config": {"mode": "single", "provider_id": "local"}})
        await authed_client.delete(f"/v1/web_fetch_providers/{jina}")
