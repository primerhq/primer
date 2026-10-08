"""import_zip refuses a zip bomb before it writes anything (FS-04).

The archive is checked as a whole first: too many entries, or a declared
uncompressed total over the cap, refuses the import with a 413 before a
single document is created. Entries are then read through a bounded
stream that counts the bytes actually decompressed, so an archive whose
headers lie about the sizes cannot get past the cap either.
"""
from __future__ import annotations

import io
import struct
import zipfile

import pytest
import pytest_asyncio

from primer.knowledge import importer
from primer.knowledge.importer import import_zip
from primer.knowledge.tree import DocumentTreeService
from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.payload_too_large import PayloadTooLargeError
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
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


async def _nothing_created(tree: DocumentTreeService, *paths: str) -> None:
    for path in paths:
        with pytest.raises(NotFoundError):
            await tree.resolve(collection_id="c1", path=path)


async def test_too_many_entries_is_refused_before_any_write(tree, monkeypatch):
    monkeypatch.setattr(importer, "MAX_ENTRIES", 3)
    data = _zip({f"d{i}.md": b"x" for i in range(4)})
    with pytest.raises(PayloadTooLargeError) as exc:
        await import_zip(tree, collection_id="c1", data=data)
    assert "4 entries" in exc.value.message
    await _nothing_created(tree, "d0", "d1", "d2", "d3")


async def test_the_entry_cap_admits_an_archive_at_the_cap(tree, monkeypatch):
    monkeypatch.setattr(importer, "MAX_ENTRIES", 3)
    data = _zip({f"d{i}.md": b"x" for i in range(3)})
    report = await import_zip(tree, collection_id="c1", data=data)
    assert sorted(report.created) == ["d0", "d1", "d2"]


async def test_a_declared_uncompressed_total_over_the_cap_is_refused(tree, monkeypatch):
    """A small archive that inflates past the cap (the zip bomb shape)."""
    monkeypatch.setattr(importer, "MAX_UNCOMPRESSED_BYTES", 1000)
    data = _zip({"small.md": b"a" * 10, "bomb.md": b"a" * 5000})
    assert len(data) < 1000  # it compresses well: the archive itself is small
    with pytest.raises(PayloadTooLargeError) as exc:
        await import_zip(tree, collection_id="c1", data=data)
    assert exc.value.limit_bytes == 1000
    await _nothing_created(tree, "small", "bomb")


def _lie_about_sizes(data: bytes, claimed: int) -> bytes:
    """Rewrite every central-directory entry's uncompressed size to ``claimed``."""
    out = bytearray(data)
    pos = out.find(b"PK\x01\x02")
    while pos != -1:
        struct.pack_into("<I", out, pos + 24, claimed)
        pos = out.find(b"PK\x01\x02", pos + 4)
    return bytes(out)


async def test_an_archive_that_lies_about_its_sizes_is_refused_cleanly(tree, monkeypatch):
    """The declared sizes pass the pre-check; the bytes actually inflated do not match."""
    monkeypatch.setattr(importer, "MAX_UNCOMPRESSED_BYTES", 1000)
    data = _lie_about_sizes(_zip({"bomb.md": b"a" * 5000}), claimed=10)
    # The declared-size pre-check passes (10 bytes); the bounded read then gets only the 10 declared bytes
    # from zipfile, whose CRC does not match, so it is the corrupt-entry guard (400) that fires.
    with pytest.raises(BadRequestError, match="cannot be read"):
        await import_zip(tree, collection_id="c1", data=data)
    await _nothing_created(tree, "bomb")


async def test_the_bounded_read_counts_the_bytes_actually_inflated(tree, monkeypatch):
    """Even if an entry yields more than it declared, the running count stops it."""
    monkeypatch.setattr(importer, "MAX_UNCOMPRESSED_BYTES", 1000)
    data = _zip({"a.md": b"a" * 600, "b.md": b"b" * 600})
    # The pre-check is told each entry is 10 bytes; the archive still inflates 600 each.
    monkeypatch.setattr(importer, "_declared_size", lambda info: 10)
    with pytest.raises(PayloadTooLargeError):
        await import_zip(tree, collection_id="c1", data=data)


async def test_path_traversal_entry_names_are_rejected(tree):
    data = _zip({"../escape.md": b"x", "a/../../b.md": b"y", "ok.md": b"z"})
    report = await import_zip(tree, collection_id="c1", data=data, parent="")
    assert report.created == ["ok"]
    assert sorted(r["file"] for r in report.rejected) == ["../escape.md", "a/../../b.md"]


async def test_import_accepts_a_spooled_file(tree):
    """The route hands over the spooled upload, not a bytes copy of it."""
    spool = io.BytesIO(_zip({"x.md": b"hello"}))
    report = await import_zip(tree, collection_id="c1", data=spool)
    assert report.created == ["x"]


def _never(*a, **k):
    raise AssertionError("the central directory was parsed")


async def test_the_entry_count_is_read_from_the_eocd_before_the_central_directory_is_parsed(tree, monkeypatch):
    """A huge central directory is refused from the end record alone, before zipfile parses it."""
    monkeypatch.setattr(importer, "MAX_ENTRIES", 3)
    data = _zip({f"d{i}.md": b"x" for i in range(4)})
    monkeypatch.setattr(importer.zipfile, "ZipFile", _never)
    with pytest.raises(PayloadTooLargeError) as exc:
        await import_zip(tree, collection_id="c1", data=data)
    assert exc.value.limit_entries == 3


async def test_the_eocd_count_is_read_from_a_zip64_end_record(tree, monkeypatch):
    monkeypatch.setattr(importer, "MAX_ENTRIES", 3)
    raw = bytearray(_zip({f"d{i}.md": b"x" for i in range(4)}))
    # Rewrite it as a zip64 archive: classic EOCD counts 0xFFFF, plus a zip64 end record and locator.
    eocd = raw.rfind(b"PK\x05\x06")
    cd_size, cd_offset = struct.unpack_from("<II", raw, eocd + 12)
    rec = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, 4, 4, cd_size, cd_offset)
    loc = struct.pack("<4sIQI", b"PK\x06\x07", 0, eocd, 1)
    end = bytearray(raw[eocd:])
    struct.pack_into("<HH", end, 8, 0xFFFF, 0xFFFF)
    data = bytes(raw[:eocd]) + rec + loc + bytes(end)
    monkeypatch.setattr(importer.zipfile, "ZipFile", _never)
    with pytest.raises(PayloadTooLargeError) as exc:
        await import_zip(tree, collection_id="c1", data=data)
    assert exc.value.limit_entries == 3
