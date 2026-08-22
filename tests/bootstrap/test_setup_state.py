"""S5 P2: setup completeness is derived live, never stamped."""
from __future__ import annotations

from primer.bootstrap.defaults import (
    RESERVED_BUILDER_AGENT,
    RESERVED_DEFAULT_WORKSPACE,
    RESERVED_OPERATOR_AGENT,
)
from primer.bootstrap.setup_state import (
    MISSING_BUILDER_AGENT,
    MISSING_DEFAULT_WORKSPACE,
    MISSING_LLM_PROVIDER,
    MISSING_MODEL_PROFILE,
    MISSING_OPERATOR_AGENT,
    MISSING_SYSTEM_COLLECTION,
    evaluate_setup_state,
)
from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection
from primer.model.model_profile import ModelProfile
from primer.model.provider import LLMProvider
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta


async def _seed_everything(sp):
    from datetime import datetime, timezone

    await sp.get_storage(LLMProvider).create(
        LLMProvider(
            id="llm-1",
            provider="ollama",
            config={"url": "http://localhost:11434"},
            limits={"max_concurrency": 2},
        )
    )
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id="llm-1--qwen",
            description="default",
            provider_id="llm-1",
            model_name="qwen",
            context_length=32000,
        )
    )
    await sp.get_storage(Workspace).create(
        Workspace(
            id=RESERVED_DEFAULT_WORKSPACE,
            template_id="local-default",
            provider_id="local",
            created_at=datetime.now(timezone.utc),
            # runtime_meta is REQUIRED on the row (workspace.py:1196-1199);
            # the values are never dialled here, only stored.
            runtime_meta=WorkspaceRuntimeMeta(
                url="ws://127.0.0.1:5959", token="seed-token",
            ),
        )
    )
    for agent_id in (RESERVED_OPERATOR_AGENT, RESERVED_BUILDER_AGENT):
        await sp.get_storage(Agent).create(
            Agent(
                id=agent_id,
                description=agent_id,
                model=AgentModel(profile_id="llm-1--qwen"),
            )
        )
    await sp.get_storage(Collection).create(
        Collection(id=SYSTEM_COLLECTION_ID, description="system", system=True)
    )


async def test_empty_install_reports_every_missing_piece(fake_storage_provider):
    fake_storage_provider.get_storage(ModelProfile)._data.clear()
    state = await evaluate_setup_state(fake_storage_provider)
    assert state.complete is False
    assert state.missing == [
        MISSING_LLM_PROVIDER,
        MISSING_MODEL_PROFILE,
        MISSING_DEFAULT_WORKSPACE,
        MISSING_OPERATOR_AGENT,
        MISSING_BUILDER_AGENT,
        MISSING_SYSTEM_COLLECTION,
    ]


async def test_fully_seeded_install_is_complete(fake_storage_provider):
    fake_storage_provider.get_storage(ModelProfile)._data.clear()
    await _seed_everything(fake_storage_provider)
    state = await evaluate_setup_state(fake_storage_provider)
    assert state.complete is True
    assert state.missing == []


async def test_a_deleted_operator_reopens_the_gate(fake_storage_provider):
    fake_storage_provider.get_storage(ModelProfile)._data.clear()
    await _seed_everything(fake_storage_provider)
    await fake_storage_provider.get_storage(Agent).delete(RESERVED_OPERATOR_AGENT)
    state = await evaluate_setup_state(fake_storage_provider)
    assert state.complete is False
    assert state.missing == [MISSING_OPERATOR_AGENT]
