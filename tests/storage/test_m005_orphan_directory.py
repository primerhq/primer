"""GR-12: m005, resumed after a crash between a directory row and its content entry, must not adopt that orphan as a parent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from primer.model.provider import SqliteConfig
from primer.storage.migrations.m004_document_slugs import Document
from primer.storage.migrations.m005_document_directories import M005DocumentDirectories
from primer.storage.sqlite import SqliteStorageProvider

CID = "kb"


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "m005.sqlite")))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _doc(sp, doc_id: str, path: str, *, content: str | None = "body") -> None:
    await sp.get_storage(Document).create(Document(id=doc_id, collection_id=CID, path=path, slug=path.rpartition("/")[2]))
    if content is not None:
        await sp.get_content_store().upsert(document_id=doc_id, collection_id=CID, path=path, content=content)


async def test_an_orphan_directory_row_is_not_adopted_and_the_children_get_a_parent_that_resolves(sp):
    await _doc(sp, "orphan-dir", "cookbook", content=None)  # the crash: the row was written, the content entry was not
    await _doc(sp, "child", "cookbook/x.md")

    await M005DocumentDirectories().apply(sp)

    child = await sp.get_storage(Document).get("child")
    resolved = await sp.get_content_store().resolve_id(CID, "cookbook")
    assert resolved is not None and child.parent_id == resolved  # the parent resolves in the content store
    assert resolved != "orphan-dir"
    assert await sp.get_storage(Document).get("orphan-dir") is None  # and the orphan row is gone


async def test_a_real_directory_row_is_still_adopted(sp):
    await _doc(sp, "dir", "cookbook", content="")
    await _doc(sp, "child", "cookbook/x.md")

    await M005DocumentDirectories().apply(sp)

    assert (await sp.get_storage(Document).get("child")).parent_id == "dir"
    assert await sp.get_content_store().resolve_id(CID, "cookbook") == "dir"


async def test_a_second_run_changes_nothing(sp):
    await _doc(sp, "orphan-dir", "cookbook", content=None)
    await _doc(sp, "child", "cookbook/x.md")
    await M005DocumentDirectories().apply(sp)
    parent = (await sp.get_storage(Document).get("child")).parent_id

    await M005DocumentDirectories().apply(sp)

    assert (await sp.get_storage(Document).get("child")).parent_id == parent
