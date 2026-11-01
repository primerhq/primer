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


def _encrypted_zip() -> bytes:
    """One entry with the encryption bit set in its local and central headers (zipfile cannot write an encrypted entry)."""
    raw = bytearray(_zip({"a.md": b"hello"}))
    raw[6] |= 1
    cd = raw.rfind(b"PK\x01\x02")
    raw[cd + 8] |= 1
    return bytes(raw)


async def test_an_encrypted_entry_is_a_bad_request(tree):
    """KN-19b: zipfile raises RuntimeError for an encrypted entry; it is a bad upload (4xx), not a server fault."""
    from primer.model.except_ import BadRequestError

    with pytest.raises(BadRequestError, match="a.md"):
        await import_zip(tree, collection_id="c1", data=_encrypted_zip())


async def test_a_fail_conflict_reports_what_was_written(tree):
    """KN-19b: the import stops at the first existing path and says what it had already written."""
    await tree.create(collection_id="c1", parent="", slug="b", body="old")
    data = _zip({"a.md": b"A", "b.md": b"B", "c.md": b"C"})

    with pytest.raises(ConflictError) as caught:
        await import_zip(tree, collection_id="c1", data=data, parent="", conflict="fail")

    assert "'b'" in caught.value.message
    assert caught.value.problem_extensions["report"]["created"] == ["a"]
    assert (await tree.read(collection_id="c1", path="a")).body == "A"
    assert (await tree.read(collection_id="c1", path="b")).body == "old"
