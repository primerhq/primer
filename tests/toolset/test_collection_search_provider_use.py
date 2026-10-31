"""The system toolset's collection doors check search provider use like the REST routes (security review of P4, findings 2 and 3).

``create_collection`` with a ``search`` block naming providers the caller may not use is refused with the not-registered answer
``enable_search`` gives, and stores nothing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from primer.model.access import Access
from primer.model.collection import Collection, CollectionEmbedder, CollectionSearchConfig
from primer.model.principal import PrincipalRef
from primer.model.provider import (
    EmbeddingModel, EmbeddingProvider, EmbeddingProviderType, HuggingFaceConfig, Limits, SemanticSearchProvider, SqliteConfig,
)
from primer.model.user import User
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.system import build_system_toolset


def _ctx(uid: str, role: str = "user") -> ToolContext:
    ref = PrincipalRef(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)
    return ToolContext(tool_call_id="c", session_id="s", workspace_id="w", initiated_by=ref)


def _cfg(suffix: str, state: str = "ready") -> CollectionSearchConfig:
    return CollectionSearchConfig(
        embedder=CollectionEmbedder(provider_id=f"emb-{suffix}", model="m"), vector_store_provider_id=f"ssp-{suffix}", state=state,
    )


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    for uid in ("ada", "root"):
        await provider.get_storage(User).create(User(id=uid, username=uid, created_at=datetime.now(UTC), role="admin" if uid == "root" else "user"))
    for suffix, access in (("priv", Access()), ("pub", Access(public=True))):
        await provider.get_storage(EmbeddingProvider).create(EmbeddingProvider(
            id=f"emb-{suffix}", provider=EmbeddingProviderType.HUGGINGFACE, models=[EmbeddingModel(name="m")],
            config=HuggingFaceConfig(token=SecretStr("hf_x")), limits=Limits(max_concurrency=1), access=access))
        await provider.get_storage(SemanticSearchProvider).create(SemanticSearchProvider.model_validate({
            "id": f"ssp-{suffix}", "provider": "pgvector", "access": access.model_dump(mode="json"),
            "config": {"hostname": "h", "port": 5432, "database": "d", "username": "u", "password": "p", "db_schema": "public"}}))
    yield provider
    await provider.aclose()


async def test_create_collection_naming_private_search_providers_is_refused(sp):
    system = build_system_toolset(storage_provider=sp, provider_registry=AsyncMock(), semantic_search_registry=AsyncMock())
    body = {"id": "mine", "description": "d", "search": _cfg("priv").model_dump(mode="json")}

    res = await system.call(tool_name="create_collection", arguments={"entity": body}, ctx=_ctx("ada"))

    assert res.is_error and json.loads(res.output)["type"] == "conflict", res.output
    assert "is not registered" in res.output
    assert await sp.get_storage(Collection).get("mine") is None


async def test_create_collection_naming_usable_search_providers_is_stored(sp):
    system = build_system_toolset(storage_provider=sp, provider_registry=AsyncMock(), semantic_search_registry=AsyncMock())
    body = {"id": "mine", "description": "d", "search": _cfg("pub").model_dump(mode="json")}

    res = await system.call(tool_name="create_collection", arguments={"entity": body}, ctx=_ctx("ada"))

    assert not res.is_error, res.output
    assert (await sp.get_storage(Collection).get("mine")).search is not None
