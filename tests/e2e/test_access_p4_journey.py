"""Runs act with their person: tool availability, model profile use and global settings, over the live API (access control, Phase 4).

Admin sets the world up; user B is the person whose runs are filtered. Three journeys:

* a toolset private to the admin is absent from B's run (the model is told ``not available to you``), and marking ONE tool public offers just that
  tool, never its sibling;
* a private model profile refuses B's turn with ``model_profile_unusable`` while the session stays (paused), and the turn runs after an admin grants use;
* a non-admin reads a global setting with its secret masked and cannot write it.

Unit coverage lives in tests/access, tests/agent, tests/model_profile and tests/session; this pins the wiring on a live server (CI only).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.model_profiles import profile_id_for
from tests._support.runs import make_local_workspace, make_scripted_agent, start_agent_session, wait_completed, wait_for_status

pytestmark = pytest.mark.asyncio

_PASSWORD = "access-journey-password-123"

TOOLS = (
    "@primer_tool()\n"
    "def greet(name: str) -> str:\n"
    '    """Greet a person by name.\n\n'
    "    Use when you need a friendly greeting.\n\n"
    "    Args:\n        name: Who to greet.\n"
    '    """\n'
    "    return 'hello ' + name\n\n"
    "@primer_tool()\n"
    "def shout(name: str) -> str:\n"
    '    """Shout a name.\n\n'
    "    Use when you need it loud.\n\n"
    "    Args:\n        name: Who to shout.\n"
    '    """\n'
    "    return name.upper()\n"
)


@asynccontextmanager
async def _user(client: httpx.AsyncClient, base_url: str, username: str) -> AsyncIterator[tuple[str, httpx.AsyncClient]]:
    made = await client.post("/v1/admin/users", json={"username": username, "password": _PASSWORD, "role": "user"})
    assert made.status_code == 201, made.text
    user_id = made.json()["id"]
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0)) as c:
            login = await c.post("/v1/auth/login", json={"username": username, "password": _PASSWORD})
            assert login.status_code == 200, login.text
            yield user_id, c
    finally:
        await client.delete(f"/v1/admin/users/{user_id}")


async def _access(client: httpx.AsyncClient, plural: str, entity_id: str, **changes) -> dict:
    """Read the sharing, apply ``changes`` to the whole desired state and write it back with the rev just read (an admin's door)."""
    view = (await client.get(f"/v1/{plural}/{entity_id}/access")).json()
    body = {
        "owners": [o["id"] for o in view["owners"]],
        "grants": {g["user"]["id"]: g["level"] for g in view["grants"]},
        "public": view["public"],
        "rev": view["rev"],
        **changes,
    }
    put = await client.put(f"/v1/{plural}/{entity_id}/access", json=body)
    assert put.status_code == 200, put.text
    return put.json()


async def _public_infra(client: httpx.AsyncClient, provider_id: str, profile_id: str) -> None:
    for plural, rid in (("llm_providers", provider_id), ("model_profiles", profile_id)):
        await _access(client, plural, rid, owners=[], public=True)


async def _share_workspace_full(client: httpx.AsyncClient, workspace_id: str, user_id: str) -> None:
    view = (await client.get(f"/v1/workspaces/{workspace_id}/access")).json()
    owners = [o["id"] for o in view["owners"]]
    await _access(client, "workspaces", workspace_id, owners=owners, grants={user_id: "full"})


def _offered(request: dict) -> list[str]:
    return [t["function"]["name"] for t in request.get("tools") or []]


def _text(request: dict) -> str:
    return json.dumps(request.get("messages") or [])


async def test_a_private_toolset_is_absent_from_a_users_run_and_a_public_tool_opens_just_that_tool(
    authed_client, base_url, mock_llm, unique_suffix, tmp_path: Path
) -> None:
    registry, mock_url = mock_llm
    tid = f"acl4-py-{unique_suffix}"
    scenario = f"scripted:acl4-tools-{unique_suffix}"
    created = await authed_client.post("/v1/toolsets", json={"id": tid, "provider": "python", "config": {"source": TOOLS, "source_version": 1}})
    assert created.status_code == 201, created.text
    agent = await make_scripted_agent(
        authed_client, registry, mock_url, suffix=f"t-{unique_suffix}", scenario=scenario,
        tools=[f"{tid}__greet", f"{tid}__shout"], rules=[Rule(emit_text="done")],
    )
    # The agent and the model it runs on are public: only the toolset is private, so it is the one thing that differs for B.
    await _public_infra(authed_client, agent["provider_id"], profile_id_for(agent["provider_id"], scenario))
    await _access(authed_client, "agents", agent["agent_id"], public=True)
    wid = await make_local_workspace(authed_client, suffix=f"t-{unique_suffix}", root=tmp_path)
    async with _user(authed_client, base_url, f"acl4-b-{unique_suffix}") as (b_id, b):
        await _share_workspace_full(authed_client, wid, b_id)
        try:
            before = len(registry.requests)
            sid = await start_agent_session(b, workspace_id=wid, agent_id=agent["agent_id"])
            assert (await wait_completed(b, sid)).get("status") != "ended"
            first = [r for r in registry.requests[before:] if r.get("model") == scenario][-1]
            assert not [n for n in _offered(first) if n.startswith(tid)], "B's run was offered a toolset B may not read"
            assert "not available to you" in _text(first) and tid in _text(first), "the model is told what is missing"

            # One tool made public: just that tool is offered.
            await _access(authed_client, "toolsets", tid, public_tools=["greet"])
            before = len(registry.requests)
            sid2 = await start_agent_session(b, workspace_id=wid, agent_id=agent["agent_id"])
            assert (await wait_completed(b, sid2)).get("status") != "ended"
            second = [r for r in registry.requests[before:] if r.get("model") == scenario][-1]
            offered = [n for n in _offered(second) if n.startswith(tid)]
            assert [n.rsplit("__", 1)[-1] for n in offered] == ["greet"], offered
            assert "some tools not available to you" in _text(second)
        finally:
            await authed_client.delete(f"/v1/toolsets/{tid}")


async def test_a_private_profile_pauses_the_turn_not_the_session_and_resumes_after_a_grant(
    authed_client, base_url, mock_llm, unique_suffix, tmp_path: Path
) -> None:
    registry, mock_url = mock_llm
    scenario = f"scripted:acl4-profile-{unique_suffix}"
    agent = await make_scripted_agent(
        authed_client, registry, mock_url, suffix=f"p-{unique_suffix}", scenario=scenario, rules=[Rule(emit_text="done")],
    )
    provider_id, profile_id = agent["provider_id"], profile_id_for(agent["provider_id"], scenario)
    # A new profile and provider are private. The agent is public, so B may run it, and B has full access to the workspace.
    await _access(authed_client, "agents", agent["agent_id"], public=True)
    wid = await make_local_workspace(authed_client, suffix=f"p-{unique_suffix}", root=tmp_path)
    async with _user(authed_client, base_url, f"acl4-c-{unique_suffix}") as (b_id, b):
        await _share_workspace_full(authed_client, wid, b_id)
        sid = await start_agent_session(b, workspace_id=wid, agent_id=agent["agent_id"])

        paused = await wait_for_status(b, sid, "paused", timeout_s=60.0)
        assert paused.get("status") == "paused", paused
        assert (paused.get("last_turn_error") or {}).get("code") == "model_profile_unusable", paused
        assert paused.get("ended_reason") is None

        # An admin grants use of the profile and its provider; the same session resumes and the turn runs.
        for plural, rid in (("llm_providers", provider_id), ("model_profiles", profile_id)):
            await _access(authed_client, plural, rid, owners=[], grants={b_id: "read"})
        resumed = await b.post(f"/v1/workspaces/{wid}/sessions/{sid}/resume")
        assert resumed.status_code in (200, 202), resumed.text
        done = await wait_completed(b, sid, timeout_s=90.0)
        assert done.get("status") != "ended" and done.get("ended_reason") is None, done
        for _ in range(60):
            row = (await b.get(f"/v1/sessions/{sid}")).json()
            if not row.get("last_turn_error"):
                break
            await asyncio.sleep(0.5)
        assert not row.get("last_turn_error"), "the resumed turn ran and cleared the refusal"


async def test_a_user_reads_a_global_setting_with_the_secret_masked_and_cannot_write_it(
    authed_client, base_url, unique_suffix
) -> None:
    pid = f"acl4-ws-{unique_suffix}"
    secret = f"tvly-secret-{unique_suffix}-abcd"
    made = await authed_client.post("/v1/web_search_providers", json={"id": pid, "provider_type": "tavily", "config": {"type": "tavily", "api_key": secret}})
    assert made.status_code == 201, made.text
    try:
        async with _user(authed_client, base_url, f"acl4-d-{unique_suffix}") as (_uid, d):
            listed = await d.get("/v1/web_search_providers")
            assert listed.status_code == 200, listed.text
            assert secret not in listed.text and pid in listed.text
            got = await d.get(f"/v1/web_search_providers/{pid}")
            assert got.status_code == 200 and secret not in got.text and "**********" in got.text

            assert (await d.put(f"/v1/web_search_providers/{pid}", json={"id": pid, "provider_type": "tavily", "config": {"type": "tavily", "api_key": "x"}})).status_code == 403
            assert (await d.delete(f"/v1/web_search_providers/{pid}")).status_code == 403
            assert (await d.post("/v1/web_search_providers", json={"id": f"{pid}-2", "provider_type": "tavily", "config": {"type": "tavily", "api_key": "x"}})).status_code == 403
    finally:
        await authed_client.delete(f"/v1/web_search_providers/{pid}")
