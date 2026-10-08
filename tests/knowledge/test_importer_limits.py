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


# ---- review round 2: a hostile end record ---------------------------------------


async def test_a_zip64_locator_with_an_overflowing_offset_is_a_400_not_a_500(tree):
    """A 40-byte upload whose zip64 locator points past 2**63 must not crash the pre-check."""
    data = (
        b"PK\x06\x07" + struct.pack("<IQI", 0, 2**64 - 1, 1)
        + b"PK\x05\x06" + struct.pack("<HHHHIIH", 0, 0, 0xFFFF, 0xFFFF, 0, 0, 0)
    )
    with pytest.raises(BadRequestError):
        await import_zip(tree, collection_id="c1", data=data)


async def test_a_lying_entry_count_is_caught_by_the_central_directory_size(tree, monkeypatch):
    """zipfile walks the central directory by its SIZE and never reads the count fields, so a count of 1
    must not let a large directory through: every central header is at least 46 bytes."""
    monkeypatch.setattr(importer, "MAX_ENTRIES", 3)
    raw = bytearray(_zip({f"d{i}.md": b"x" for i in range(4)}))
    eocd = raw.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", raw, eocd + 8, 1, 1)
    monkeypatch.setattr(importer.zipfile, "ZipFile", _never)
    with pytest.raises(PayloadTooLargeError) as exc:
        await import_zip(tree, collection_id="c1", data=bytes(raw))
    assert exc.value.limit_entries == 3


async def test_a_zip64_record_behind_a_small_classic_count_is_still_read(tree, monkeypatch):
    """zipfile prefers a zip64 end record whenever its locator is present, whatever the classic count says."""
    monkeypatch.setattr(importer, "MAX_ENTRIES", 3)
    raw = bytearray(_zip({f"d{i}.md": b"x" for i in range(4)}))
    eocd = raw.rfind(b"PK\x05\x06")
    cd_size, cd_offset = struct.unpack_from("<II", raw, eocd + 12)
    rec = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, 4, 4, cd_size, cd_offset)
    loc = struct.pack("<4sIQI", b"PK\x06\x07", 0, eocd, 1)
    end = bytearray(raw[eocd:])
    struct.pack_into("<HH", end, 8, 1, 1)  # the classic record claims one entry
    data = bytes(raw[:eocd]) + rec + loc + bytes(end)
    monkeypatch.setattr(importer.zipfile, "ZipFile", _never)
    with pytest.raises(PayloadTooLargeError):
        await import_zip(tree, collection_id="c1", data=data)


async def test_the_parsed_entry_count_is_checked_when_the_end_record_cannot_be_read_first(tree, monkeypatch):
    """The post-parse check is the backstop for an end record the pre-check could not read.

    With a readable record it cannot be reached: size_cd // 46 >= the real number of entries, so the size
    bound already refuses any archive the parse would. So the pre-check is made to read nothing here.
    """
    monkeypatch.setattr(importer, "MAX_ENTRIES", 3)
    monkeypatch.setattr(importer, "_central_directory_bounds", lambda source: None)
    data = _zip({f"d{i}.md": b"x" for i in range(4)})
    with pytest.raises(PayloadTooLargeError) as exc:
        await import_zip(tree, collection_id="c1", data=data)
    assert exc.value.limit_entries == 3
    await _nothing_created(tree, "d0", "d1", "d2", "d3")


async def test_a_zip64_record_is_honoured_when_the_classic_record_lies_small(tree, monkeypatch):
    """The classic record claims one entry in a 46-byte directory; only the zip64 record tells the truth.

    No classic sentinel (0xFFFF / 0xFFFFFFFF) is left for the classic bound to catch, so the refusal can
    only come from reading the zip64 end record.
    """
    raw = bytearray(_zip({"a.md": b"x"}))
    eocd = raw.rfind(b"PK\x05\x06")
    cd_size, cd_offset = struct.unpack_from("<II", raw, eocd + 12)
    huge = 1_000_000
    assert huge > importer.MAX_ENTRIES
    rec = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, huge, huge, cd_size, cd_offset)
    loc = struct.pack("<4sIQI", b"PK\x06\x07", 0, eocd, 1)
    end = bytearray(raw[eocd:])
    struct.pack_into("<HHI", end, 8, 1, 1, 46)  # the classic record: one entry, a 46-byte directory
    data = bytes(raw[:eocd]) + rec + loc + bytes(end)
    monkeypatch.setattr(importer.zipfile, "ZipFile", _never)
    with pytest.raises(PayloadTooLargeError) as exc:
        await import_zip(tree, collection_id="c1", data=data)
    assert f"{huge} entries" in exc.value.message
