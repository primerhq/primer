"""A new workspace arrives with a session to talk in (S1 P5 Task 29).

"main" is an ORDINARY session: deletable, no reserved id, no flag, and
no special-casing downstream. Its only distinction is that it exists,
so opening a fresh workspace does not begin with paperwork.

Seeding is best effort. Before bootstrap stamps a default agent there
is nothing to bind to, and failing workspace creation over a
convenience would make the programme unusable across that window.
"""

from __future__ import annotations

import pytest

from primer.model.workspace_session import SessionStatus, WorkspaceSession


async def _seed_default_agent(app, aid="operator"):
    from primer.model.agent import Agent, AgentModel

    sp = app.state.storage_provider
    if await sp.get_storage(Agent).get(aid) is None:
        await sp.get_storage(Agent).create(
            Agent(id=aid, description=aid,
                  model=AgentModel(profile_id="p--m"), tools=[],
                  system_prompt=[]),
        )
    await sp.set_default_agent_id(aid)


async def _sessions_for(app, wid: str) -> list[WorkspaceSession]:
    from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value

    page = await app.state.storage_provider.get_storage(
        WorkspaceSession
    ).find(
        Predicate(left=FieldRef(name="workspace_id"), op=Op.EQ,
                  right=Value(value=wid)),
        OffsetPage(offset=0, length=50),
    )
    return list(page.items)


async def _seed_template(client, root: str):
    """A workspace needs a provider and a template to materialise from.

    The root is per-test: a shared fixed path makes a second run collide
    with the first run's directories.
    """
    from primer.model.workspace import (
        LocalWorkspaceConfig,
        WorkspaceProvider,
        WorkspaceProviderType,
        WorkspaceTemplate,
    )

    await client.post(
        "/v1/workspace_providers",
        json=WorkspaceProvider(
            id="local-1",
            provider=WorkspaceProviderType.LOCAL,
            config=LocalWorkspaceConfig(root_path=root),
        ).model_dump(mode="json"),
    )
    await client.post(
        "/v1/workspace_templates",
        json=WorkspaceTemplate(
            id="tpl-seed", description="seed test", provider_id="local-1",
        ).model_dump(mode="json"),
    )


async def _create_workspace(client, wid: str, root: str):
    await _seed_template(client, root)
    return await client.post(
        "/v1/workspaces",
        json={"id": wid, "template_id": "tpl-seed"},
    )


@pytest.mark.asyncio
async def test_seeds_exactly_one_ordinary_session_named_main(
    client, app, tmp_path,
):
    await _seed_default_agent(app)
    r = await _create_workspace(client, "ws-seeded", str(tmp_path))
    assert r.status_code < 400, r.text

    rows = await _sessions_for(app, "ws-seeded")
    assert len(rows) == 1
    main = rows[0]
    assert main.name == "main"
    assert main.binding.agent_id == "operator"
    assert main.status is SessionStatus.CREATED
    assert main.turn_status == "idle"


@pytest.mark.asyncio
async def test_no_default_agent_still_creates_the_workspace(
    client, app, tmp_path,
):
    """The S1-to-S5 window: no default yet, so no session, but the
    workspace must still be usable."""
    await app.state.storage_provider.set_default_agent_id(None)
    r = await _create_workspace(client, "ws-bare", str(tmp_path))
    assert r.status_code < 400, r.text

    assert await _sessions_for(app, "ws-bare") == []


@pytest.mark.asyncio
async def test_the_seeded_session_is_deletable_like_any_other(
    client, app, tmp_path,
):
    """No reserved id, no protection: "main" is ordinary."""
    await _seed_default_agent(app)
    r = await _create_workspace(client, "ws-del", str(tmp_path))
    assert r.status_code < 400, r.text

    rows = await _sessions_for(app, "ws-del")
    assert len(rows) == 1
    await app.state.storage_provider.get_storage(WorkspaceSession).delete(
        rows[0].id
    )
    assert await _sessions_for(app, "ws-del") == []
