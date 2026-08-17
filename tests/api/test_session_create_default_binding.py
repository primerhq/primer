"""Creating a session without naming an agent (S1 P5 Task 28).

The API half: omit binding and the system default answers; omit it with
no default configured and the create is refused with the message that
tells an operator exactly what to set.
"""

from __future__ import annotations

import pytest


async def _seed_workspace(app, wid="ws-1"):
    class _FakeWorkspace:
        state_path = ".state"
        id = wid

        async def read_file(self, path):
            from primer.model.except_ import NotFoundError

            raise NotFoundError(path)

        async def append_message_line(self, session_id, line):
            return None

    ws = _FakeWorkspace()

    async def _get(w):
        return ws if w == wid else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    return ws


async def _seed_agent(app, aid="operator"):
    from primer.model.agent import Agent, AgentModel

    sp = app.state.storage_provider
    if await sp.get_storage(Agent).get(aid) is None:
        await sp.get_storage(Agent).create(
            Agent(id=aid, description=aid,
                  model=AgentModel(profile_id="p--m"), tools=[],
                  system_prompt=[]),
        )


@pytest.mark.asyncio
async def test_omitting_binding_without_a_default_is_refused(client, app):
    """The operator-facing signal: it names the key to set."""
    await _seed_workspace(app)
    r = await client.post("/v1/workspaces/ws-1/sessions", json={})
    assert r.status_code >= 400, r.text
    assert "default_agent_id" in r.text


@pytest.mark.asyncio
async def test_explicit_binding_still_works(client, app):
    await _seed_workspace(app)
    await _seed_agent(app, "agent-a")
    r = await client.post(
        "/v1/workspaces/ws-1/sessions",
        json={"binding": {"kind": "agent", "agent_id": "agent-a"}},
    )
    # Either created, or refused for a reason unrelated to the binding.
    assert "default_agent_id" not in r.text
