"""grep_collection: regex line scan over content-store bodies."""
from __future__ import annotations

import pytest
import pytest_asyncio

from primer.knowledge.grep import grep_collection
from primer.model.except_ import BadRequestError
from primer.model.provider import (
    SqliteConfig, StorageProviderConfig, StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory


@pytest_asyncio.fixture
async def store(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    cs = provider.get_content_store()
    await cs.ensure_schema()
    await cs.upsert(document_id="d1", collection_id="c1", path="a",
                    content="alpha\nneedle here\nomega")
    await cs.upsert(document_id="d2", collection_id="c1", path="sub/b",
                    content="no match")
    await cs.upsert(document_id="d3", collection_id="c1", path="sub/c",
                    content="needle again")
    yield cs
    await provider.aclose()


async def test_hits_carry_path_line_excerpt(store):
    res = await grep_collection(store, collection_id="c1", pattern="needle")
    assert [(h.path, h.line) for h in res.hits] == [("a", 2), ("sub/c", 1)]
    assert res.truncated is False


async def test_path_prefix_filters(store):
    res = await grep_collection(
        store, collection_id="c1", pattern="needle", path_prefix="sub/",
    )
    assert [h.path for h in res.hits] == ["sub/c"]


async def test_cap_sets_truncated(store):
    res = await grep_collection(store, collection_id="c1", pattern=".", max_results=1)
    assert len(res.hits) == 1 and res.truncated is True


async def test_bad_regex_is_bad_request(store):
    with pytest.raises(BadRequestError):
        await grep_collection(store, collection_id="c1", pattern="(unclosed")
