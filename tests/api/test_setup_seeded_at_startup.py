"""S5 P3: a booted app has the seeded world (lifespan parity in conftest)."""
from __future__ import annotations

from primer.bootstrap.defaults import (
    RESERVED_BUILDER_AGENT,
    RESERVED_OPERATOR_AGENT,
)
from primer.bootstrap.setup_state import MISSING_DEFAULT_WORKSPACE
from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID
from primer.model.agent import Agent
from primer.model.collection import Collection


async def test_the_seeded_agents_exist_after_startup(app):
    agents = app.state.storage_provider.get_storage(Agent)
    assert await agents.get(RESERVED_OPERATOR_AGENT) is not None
    assert await agents.get(RESERVED_BUILDER_AGENT) is not None


async def test_the_system_collection_exists_after_startup(app):
    collections = app.state.storage_provider.get_storage(Collection)
    assert await collections.get(SYSTEM_COLLECTION_ID) is not None


async def test_default_agent_id_points_at_the_operator(app):
    state = await app.state.storage_provider.get_system_state()
    assert state.default_agent_id == RESERVED_OPERATOR_AGENT


async def test_status_names_only_the_workspace_as_missing(client, app):
    """The predicate stays HONEST about this fixture.

    ``create_test_app`` builds a real ``WorkspaceRegistry`` over the fake
    storage (app.py:176-177) but the fixture seeds no WorkspaceProvider /
    WorkspaceTemplate rows, so ``ensure_default_workspace`` defers rather
    than materialising (Task 11's third case). Everything else IS seeded,
    so the one remaining code is the honest answer, not a bug: a test that
    asserted ``setup_complete is True`` here would only be asserting that
    the predicate lies.
    """
    from primer.model.provider import LLMProvider

    await app.state.storage_provider.get_storage(LLMProvider).create(
        LLMProvider(
            id="llm-1",
            provider="ollama",
            config={"url": "http://localhost:11434"},
            limits={"max_concurrency": 2},
        )
    )
    r = await client.get("/v1/auth/status")
    assert r.json()["setup_missing"] == [MISSING_DEFAULT_WORKSPACE]
    assert r.json()["setup_complete"] is False
