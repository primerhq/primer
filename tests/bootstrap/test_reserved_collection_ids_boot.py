"""The platform still materialises the collections whose ids a client may not take (ticket 01a16cad, lead ruling 2).

``check_collection_id`` now refuses a new collection whose id starts with ``_internal_`` or is the platform wiki's (``system``). That check
runs only in the client doors (the REST create hook and the ``create_collection`` tool). The two boot paths write their rows straight to
storage and never meet it: the internal collections bootstrap (``InternalCollectionsSubsystem._materialise_collection_rows``) and the
system-collection seed (``primer.knowledge.system_collection._ensure_collection``, run by ``regenerate_system_collection``, which the
ensure pass ends with). This boots both on a real SQLite store, twice (the second pass is the update path), and reads every row back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.common.entity_checks import EntityCheckError
from primer.internal_collections import build_subsystem
from primer.knowledge.checks import check_collection_id
from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID, regenerate_system_collection
from primer.model.collection import Collection
from primer.model.internal import INTERNAL_COLLECTION_IDS, INTERNAL_COLLECTIONS_CONFIG_ID, InternalCollectionsConfig
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from tests.test_internal_collections import _FakeEmbedder, _FakePR, _FakeSSR, _FakeVectorStore


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "boot.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    yield provider
    await provider.aclose()


async def _boot(sp) -> None:
    subsystem = build_subsystem(
        config=InternalCollectionsConfig(
            id=INTERNAL_COLLECTIONS_CONFIG_ID, embedding_provider_id="hf-1", embedding_model="m", search_provider_id="ssp-test",
        ),
        storage_provider=sp,
        provider_registry=_FakePR(_FakeEmbedder()),  # type: ignore[arg-type]
        semantic_search_registry=_FakeSSR(_FakeVectorStore()),  # type: ignore[arg-type]
    )
    try:
        assert (await subsystem.bootstrap())["ok"] is True
    finally:
        await subsystem.aclose()
    await regenerate_system_collection(sp, toolset_providers={})


@pytest.mark.asyncio
async def test_the_boot_materialises_every_internal_collection_and_the_system_collection(sp) -> None:
    every_reserved = [*INTERNAL_COLLECTION_IDS.values(), SYSTEM_COLLECTION_ID]
    for cid in every_reserved:
        with pytest.raises(EntityCheckError, match="reserved for the platform"):
            check_collection_id(Collection(id=cid, description="x"))  # the rule a client meets holds for every one of them

    for _ in range(2):
        await _boot(sp)
        for cid in every_reserved:
            row = await sp.get_storage(Collection).get(cid)
            assert row is not None, f"the boot did not materialise {cid!r}"
            assert row.system is True, f"{cid!r} is not a system row"
