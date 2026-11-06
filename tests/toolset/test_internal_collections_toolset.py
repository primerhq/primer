"""internal_collections toolset: the IC subsystem's admin surface, unit guards.

The five tools mirror the admin REST routes 1:1; this file pins the
guards the mirrors carry (the live end-to-end flow is the e2e test
`test_m2_enable_flow_on_seeded_rows_roundtrip` in
tests/e2e/test_internal_collections_lifecycle.py and the D1 probe
evidence): the SSP-existence validation, the frozen vector-space
fields, the activation stamping, the not-configured answers, and the
idle status synthesis.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from primer.model.collection import Collection
from primer.model.internal import (
    INTERNAL_COLLECTIONS_CONFIG_ID,
    InternalCollectionsConfig,
)
from primer.model.provider import EmbeddingProvider
from primer.model.providers.vector import SemanticSearchProvider
from primer.model.collection import CollectionSearchConfig
from primer.toolset.internal_collections import (
    INTERNAL_COLLECTIONS_TOOLSET_ID,
    build_internal_collections_toolset,
)
from primer.storage.factory import StorageProviderFactory
from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType
from tests._support.caller import AsCaller

_SYSTEM_ARGS = {
    "embedding_provider_id": "huggingface",
    "embedding_model": "BAAI/bge-small-en-v1.5",
    "search_provider_id": "lance",
}


@pytest_asyncio.fixture
async def env(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "ic.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    # The system collection (bootstrap's target) + the seeded rows the
    # runbook's preconditions read.
    colls = provider.get_storage(Collection)
    await colls.create(Collection(id="system", description="system", system=True))
    ssps = provider.get_storage(SemanticSearchProvider)
    await ssps.create(SemanticSearchProvider(
        id="lance", provider="lance", config={"path": str(tmp_path / "lance")},
    ))
    embs = provider.get_storage(EmbeddingProvider)
    await embs.create(EmbeddingProvider(
        id="huggingface", provider="huggingface",
        models=[{"name": "BAAI/bge-small-en-v1.5"}],
        config={"token": ""}, limits={"max_concurrency": 1},
    ))
    ts = build_internal_collections_toolset(
        storage_provider=provider,
        provider_registry=None,
        semantic_search_registry=None,
    )
    env = AsCaller(ts)
    env.storage = provider  # the test's direct storage handle (activation stamping)
    yield env
    await provider.aclose()


async def test_list_tools_five_admin(env):
    tools = [t async for t in env.list_tools()]
    assert {t.id for t in tools} == {
        "get_config", "set_config", "delete_config", "bootstrap", "bootstrap_status",
    }
    assert all(t.toolset_id == INTERNAL_COLLECTIONS_TOOLSET_ID for t in tools)
    assert all(t.required_role == "admin" for t in tools)


async def test_get_config_absent_answers_not_configured(env):
    res = await env.call(tool_name="get_config", arguments={})
    assert not res.is_error
    assert json.loads(res.output) == {"configured": False}


async def test_set_config_refuses_unknown_ssp(env):
    res = await env.call(tool_name="set_config", arguments={
        **_SYSTEM_ARGS, "search_provider_id": "no-such-ssp",
    })
    assert res.is_error
    body = json.loads(res.output)
    assert body["type"] == "not-found"
    assert "no-such-ssp" in body["message"]


async def test_set_config_writes_the_row(env):
    res = await env.call(tool_name="set_config", arguments=_SYSTEM_ARGS)
    assert not res.is_error, res.output
    row = json.loads(res.output)
    assert row["id"] == INTERNAL_COLLECTIONS_CONFIG_ID
    assert row["embedding_provider_id"] == "huggingface"
    assert row["embedding_model"] == "BAAI/bge-small-en-v1.5"
    assert row["search_provider_id"] == "lance"
    assert row["activated_at"] is None
    # And get_config reads it back.
    again = await env.call(tool_name="get_config", arguments={})
    assert json.loads(again.output)["embedding_model"] == "BAAI/bge-small-en-v1.5"


async def test_set_config_freezes_vector_space_fields_once_active(env):
    await env.call(tool_name="set_config", arguments=_SYSTEM_ARGS)
    # Stamp the activation the way bootstrap does.
    # Activate through storage directly: the tool's own stamping happens
    # inside bootstrap, which this unit env does not run (no real embedder).
    from primer.model.internal import InternalCollectionsConfig as _IC
    from datetime import datetime, timezone

    storage = env.storage.get_storage(_IC)
    existing = await storage.get(INTERNAL_COLLECTIONS_CONFIG_ID)
    await storage.update(existing.model_copy(
        update={"activated_at": datetime.now(timezone.utc)},
    ))

    # A different embedding model is refused, naming the frozen field.
    res = await env.call(tool_name="set_config", arguments={
        **_SYSTEM_ARGS, "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
    })
    assert res.is_error
    body = json.loads(res.output)
    assert body["type"] == "conflict"
    assert "embedding_model" in body["message"]

    # An identical rewrite is a no-op, not a conflict.
    same = await env.call(tool_name="set_config", arguments=_SYSTEM_ARGS)
    assert not same.is_error, same.output

    # The reranker stays editable while active.
    reranked = await env.call(tool_name="set_config", arguments={
        **_SYSTEM_ARGS,
        "cross_encoder": {"provider_id": "reranker-1", "model": "BAAI/bge-reranker-v2-m3"},
    })
    assert not reranked.is_error, reranked.output
    assert json.loads(reranked.output)["cross_encoder"]["provider_id"] == "reranker-1"


async def test_bootstrap_refuses_without_config(env):
    res = await env.call(tool_name="bootstrap", arguments={})
    assert res.is_error
    assert json.loads(res.output)["type"] == "not-found"


async def test_delete_config_refuses_when_absent(env):
    res = await env.call(tool_name="delete_config", arguments={})
    assert res.is_error
    assert json.loads(res.output)["type"] == "not-found"


async def test_delete_config_deactivates(env):
    await env.call(tool_name="set_config", arguments=_SYSTEM_ARGS)
    res = await env.call(tool_name="delete_config", arguments={})
    assert not res.is_error, res.output
    assert json.loads(res.output)["deleted"] is True
    gone = await env.call(tool_name="get_config", arguments={})
    assert json.loads(gone.output) == {"configured": False}


async def test_bootstrap_status_synthesizes_idle(env):
    res = await env.call(tool_name="bootstrap_status", arguments={})
    assert not res.is_error, res.output
    body = json.loads(res.output)
    # No search configured on the system collection yet.
    assert body["state"] == "disabled"
    assert body["status"] == "idle"
    assert body["documents_total"] == 0


async def test_set_config_rejects_malformed_arguments(env):
    res = await env.call(tool_name="set_config", arguments={})
    assert res.is_error
    assert json.loads(res.output)["type"] == "validation-error"


async def test_the_writes_ship_the_admin_tier_gate(env):
    """Condition 7 (the lead's ruling): the three writes carry the required
    approval gate, decided by an admin only; the two reads are ungated."""
    from primer.model.tool_approval import ApproverSpec
    from primer.session.approvers import ADMIN_ONLY_SPEC
    from primer.toolset.crud import shipped_approvers, shipped_gate
    from primer.toolset.tool_catalog import default_gate

    for name in ("set_config", "delete_config", "bootstrap"):
        assert default_gate(INTERNAL_COLLECTIONS_TOOLSET_ID, name) == "required", name
        assert shipped_gate(
            INTERNAL_COLLECTIONS_TOOLSET_ID, name,
        ).type.value == "required", name
        assert shipped_approvers(
            INTERNAL_COLLECTIONS_TOOLSET_ID, name,
        ) == ApproverSpec.model_validate(ADMIN_ONLY_SPEC), name
    for name in ("get_config", "bootstrap_status"):
        assert shipped_gate(INTERNAL_COLLECTIONS_TOOLSET_ID, name) is None, name
        assert default_gate(INTERNAL_COLLECTIONS_TOOLSET_ID, name) is None, name
