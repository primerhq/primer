"""Journey: a workspace's agents.md reaches the agent's system prompt on every turn (pivot P2, Task 8).

A real server over HTTP, the real OpenChatLLM client and the scripted mock, which keeps the body of every request it is sent
(``registry.requests``), so the test reads the system prompt the model was actually given. The file is written, edited, deleted and
replaced by a directory through the workspace file API; each step is followed by a turn:

* the first turn's system prompt carries the file after the agent's own prompt and the workspace section, and its braces are text;
* an edit reaches the next turn, with no change to the agent;
* a deleted file is gone from the turn after it, and the turn runs;
* a directory named agents.md is skipped, and the turn runs.

Lead-scheduled: e2e runs are CPU-exclusive (AGENTS.md section 5).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import make_local_workspace, make_scripted_agent, start_agent_session, wait_completed

_HEADING = "## Workspace instructions (agents.md)"
# The agent's own prompt holds a Jinja expression: it is rendered for the model, so a render that failed (and fell back to
# the raw join) shows in the assertions below.
_AGENT_PROMPT_TEMPLATE = "You are a scripted test agent for workspace {{ ctx.workspace_id }}."
# Valid Jinja on purpose: left unescaped it renders without an error, to ``2 y`` and no comment. A text that raised (an
# unknown tag, an undefined name) would send the renderer to its raw-join fallback, which returns the file text byte for
# byte, so a check on such a text could not tell an escaped file from an unescaped one.
_FILE_V1 = "Answer in French.\nSample: {{ 1 + 1 }} {# note #} {% if true %}y{% endif %} {"
_FILE_V2 = "Answer in German."


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in (content or []) if isinstance(part, dict))


def _system_prompts(registry, scenario: str) -> list[str]:
    """The system prompt of every request made to ``scenario``, oldest first."""
    prompts = []
    for request in registry.requests:
        if request.get("model") != scenario:
            continue
        system = [m for m in request.get("messages", []) if m.get("role") == "system"]
        prompts.append("\n".join(_content_text(m.get("content")) for m in system))
    return prompts


async def _wait_for_prompts(registry, scenario: str, count: int, *, timeout_s: float = 45.0) -> list[str]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    prompts = _system_prompts(registry, scenario)
    while len(prompts) < count and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.25)
        prompts = _system_prompts(registry, scenario)
    assert len(prompts) >= count, f"only {len(prompts)} model requests were made, wanted {count}"
    return prompts


async def _put(client: httpx.AsyncClient, workspace_id: str, path: str, content: str) -> None:
    r = await client.put(
        f"/v1/workspaces/{workspace_id}/files", params={"path": path}, json={"content": content, "encoding": "text"},
    )
    assert r.status_code == 204, r.text


async def _next_turn(client: httpx.AsyncClient, workspace_id: str, session_id: str, instruction: str) -> None:
    r = await client.post(
        f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer", json={"instruction": instruction},
    )
    assert r.status_code in (200, 201, 202), r.text


async def _rested(client: httpx.AsyncClient, session_id: str) -> None:
    """The turn finished cleanly: the interactive session rests parked, with no failure recorded."""
    row = await wait_completed(client, session_id)
    assert row.get("session_state") == "parked" and not row.get("last_turn_error"), row


@pytest.mark.asyncio
async def test_agents_md_reaches_every_turn_and_never_fails_one(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    registry, base_url = mock_llm
    scenario = f"scripted:agents-md-{unique_suffix}"
    agent = await make_scripted_agent(
        client, registry, base_url, suffix=unique_suffix, scenario=scenario,
        rules=[Rule(emit_text="noted")], system_prompt=[_AGENT_PROMPT_TEMPLATE],
    )
    workspace_id = await make_local_workspace(client, suffix=unique_suffix, root=tmp_path)
    agent_prompt = f"You are a scripted test agent for workspace {workspace_id}."
    await _put(client, workspace_id, "agents.md", _FILE_V1)

    # Turn 1: the file is in the system prompt, after the agent's own prompt and the workspace section, as written.
    session_id = await start_agent_session(
        client, workspace_id=workspace_id, agent_id=agent["agent_id"], instructions="first",
    )
    first = (await _wait_for_prompts(registry, scenario, 1))[0]
    await _rested(client, session_id)
    assert first.startswith(agent_prompt), f"the agent's own prompt is rendered, and first: {first[:120]!r}"
    assert first.endswith(f"{_HEADING}\n\n{_FILE_V1}"), f"the file is not rendered: {first[-160:]!r}"
    assert first.index(session_id) < first.index(_HEADING), "the workspace section (which names the session), then the file"

    # Turn 2: an edit reaches the next turn; nothing else changed.
    await _put(client, workspace_id, "agents.md", _FILE_V2)
    await _next_turn(client, workspace_id, session_id, "second")
    second = (await _wait_for_prompts(registry, scenario, 2))[1]
    await _rested(client, session_id)
    assert second.endswith(f"{_HEADING}\n\n{_FILE_V2}"), second
    assert "French" not in second, "the old text is gone"

    # Turn 3: a deleted file is gone from the next prompt, and the turn runs.
    deleted = await client.delete(f"/v1/workspaces/{workspace_id}/files", params={"path": "agents.md"})
    assert deleted.status_code == 204, deleted.text
    await _next_turn(client, workspace_id, session_id, "third")
    third = (await _wait_for_prompts(registry, scenario, 3))[2]
    await _rested(client, session_id)
    assert _HEADING not in third and agent_prompt in third, third

    # Turn 4: a directory named agents.md is skipped (a warning in the server log), and the turn runs.
    made = await client.post(f"/v1/workspaces/{workspace_id}/files/dir", params={"path": "agents.md"})
    assert made.status_code == 204, made.text
    await _next_turn(client, workspace_id, session_id, "fourth")
    fourth = (await _wait_for_prompts(registry, scenario, 4))[3]
    await _rested(client, session_id)
    assert _HEADING not in fourth and agent_prompt in fourth, fourth
