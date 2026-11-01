"""The platform still materialises the collection whose id a client may not take (ticket 01a16cad, lead ruling 2).

``check_collection_id`` refuses a new collection whose id starts with ``_internal_`` or is the platform wiki's (``system``). That check
runs only in the client doors (the REST create hook and the ``create_collection`` tool). The boot path writes its row straight to storage and
never meets it: the system-collection seed (``primer.knowledge.system_collection._ensure_collection``, run by ``regenerate_system_collection``,
which the ensure pass ends with). The ``_internal_*`` ids stay reserved (an install may hold rows from the removed internal-collections
bootstrap, D18). This boots the seed on a real SQLite store, twice (the second pass is the update path), and reads the row back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.common.entity_checks import EntityCheckError
from primer.knowledge.checks import check_collection_id
from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID, regenerate_system_collection
from primer.model.collection import Collection
from primer.model.internal import INTERNAL_COLLECTION_IDS
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "boot.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    yield provider
    await provider.aclose()


async def _boot(sp) -> None:
    await regenerate_system_collection(sp, toolset_providers={})


@pytest.mark.asyncio
async def test_the_boot_materialises_the_system_collection_and_every_reserved_id_stays_refused(sp) -> None:
    every_reserved = [*INTERNAL_COLLECTION_IDS.values(), SYSTEM_COLLECTION_ID]
    for cid in every_reserved:
        with pytest.raises(EntityCheckError, match="reserved for the platform"):
            check_collection_id(Collection(id=cid, description="x"))  # the rule a client meets holds for every one of them

    for _ in range(2):
        await _boot(sp)
        row = await sp.get_storage(Collection).get(SYSTEM_COLLECTION_ID)
        assert row is not None and row.system is True, "the boot did not materialise the system collection"
