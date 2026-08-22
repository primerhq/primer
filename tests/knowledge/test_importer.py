"""import_zip: archive directory structure maps onto the document tree."""
from __future__ import annotations

import io
import zipfile

import pytest
import pytest_asyncio

from primer.knowledge.importer import import_zip, slugify_segment
from primer.knowledge.tree import DocumentTreeService
from primer.model.except_ import ConflictError
from primer.model.provider import (
    SqliteConfig, StorageProviderConfig, StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory


@pytest_asyncio.fixture
async def tree(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    yield DocumentTreeService(provider)
    await provider.aclose()


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_slugify_segment():
    assert slugify_segment("My Doc.md") == "my-doc"
    assert slugify_segment("API_notes.txt") == "api-notes"
    assert slugify_segment("...") is None


async def test_directory_structure_maps_to_tree(tree):
    data = _zip({"Guides/Intro.md": b"# hi", "Guides/deep/One.md": b"1"})
    report = await import_zip(tree, collection_id="c1", data=data,
                              parent="", conflict="fail")
    assert sorted(report.created) == [
        "guides", "guides/deep", "guides/deep/one", "guides/intro",
    ]
    assert (await tree.read(collection_id="c1", path="guides/intro")).body == "# hi"


async def test_binary_rejected_naming_file(tree):
    data = _zip({"ok.md": b"fine", "logo.png": b"\x89PNG\x00\x01"})
    report = await import_zip(tree, collection_id="c1", data=data,
                              parent="", conflict="fail")
    assert report.created == ["ok"]
    assert report.rejected[0]["file"] == "logo.png"


async def test_conflict_policies(tree):
    await tree.create(collection_id="c1", parent="", slug="a", body="old")
    data = _zip({"a.md": b"new"})
    with pytest.raises(ConflictError):
        await import_zip(tree, collection_id="c1", data=data, parent="", conflict="fail")
    r = await import_zip(tree, collection_id="c1", data=data, parent="", conflict="skip")
    assert r.skipped == ["a"]
    r = await import_zip(tree, collection_id="c1", data=data, parent="", conflict="overwrite")
    assert r.overwritten == ["a"]
    assert (await tree.read(collection_id="c1", path="a")).body == "new"
