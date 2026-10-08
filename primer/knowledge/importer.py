"""Bulk import: zip directory structure -> document tree."""
from __future__ import annotations

import io
import re
import struct
import zipfile
from typing import BinaryIO, Literal

from pydantic import BaseModel, Field

from primer.knowledge.tree import DocumentTreeService
from primer.model.except_ import BadRequestError, ConflictError, NotFoundError
from primer.model.payload_too_large import PayloadTooLargeError

_STRIP_EXT = re.compile(r"\.(md|markdown|txt|text)$", re.IGNORECASE)
_NON_SLUG = re.compile(r"[^a-z0-9-]+")

# Zip-bomb caps (FS-04). Read at call time, so a test can lower them.
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
"""Largest uploaded archive the import route accepts (enforced while the upload is read)."""
MAX_ENTRIES = 10_000
"""Most entries (files and directories) one archive may list."""
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
"""Most bytes all of an archive's entries may inflate to, declared and actually decompressed."""
_READ_CHUNK = 64 * 1024


class ImportReport(BaseModel):
    created: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    overwritten: list[str] = Field(default_factory=list)
    rejected: list[dict] = Field(default_factory=list)


def slugify_segment(raw: str) -> str | None:
    s = _STRIP_EXT.sub("", raw.strip().lower())
    s = _NON_SLUG.sub("-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s or None


async def _ensure_dir(tree: DocumentTreeService, collection_id: str,
                      parent: str, slug: str, report: ImportReport) -> str:
    path = f"{parent}/{slug}" if parent else slug
    try:
        await tree.resolve(collection_id=collection_id, path=path)
    except NotFoundError:
        await tree.create(collection_id=collection_id, parent=parent,
                          slug=slug, body="")
        report.created.append(path)
    return path


_EOCD_SIG = b"PK\x05\x06"
_ZIP64_LOCATOR_SIG = b"PK\x06\x07"
_ZIP64_EOCD_SIG = b"PK\x06\x06"
_EOCD_MIN = 22
_EOCD_SEARCH = _EOCD_MIN + 0xFFFF  # the record plus the longest possible archive comment


def _eocd_entry_count(source: BinaryIO) -> int | None:
    """The total entry count from the end-of-central-directory record, or None when it cannot be read.

    Read before ``zipfile.ZipFile`` parses the central directory, which builds one ZipInfo per entry:
    an archive listing millions of tiny entries is refused from a few bytes at its end. A zip64
    archive (classic count 0xFFFF) is followed to its zip64 end record. Anything malformed returns
    None and is left to zipfile, after which the parsed count is checked again.
    """
    try:
        source.seek(0, io.SEEK_END)
        size = source.tell()
        start = max(0, size - _EOCD_SEARCH)
        source.seek(start)
        tail = source.read(size - start)
        pos = tail.rfind(_EOCD_SIG)
        if pos < 0 or len(tail) - pos < _EOCD_MIN:
            return None
        (total,) = struct.unpack_from("<H", tail, pos + 10)
        if total != 0xFFFF:
            return total
        loc = start + pos - 20
        if loc < 0:
            return None
        source.seek(loc)
        locator = source.read(20)
        if len(locator) != 20 or locator[:4] != _ZIP64_LOCATOR_SIG:
            return None
        (rec_offset,) = struct.unpack_from("<Q", locator, 8)
        source.seek(rec_offset)
        record = source.read(56)
        if len(record) != 56 or record[:4] != _ZIP64_EOCD_SIG:
            return None
        (total64,) = struct.unpack_from("<Q", record, 32)
        return total64
    except (OSError, struct.error, ValueError):
        return None
    finally:
        try:
            source.seek(0)
        except (OSError, ValueError):
            pass


def _refuse_entry_count(count: int) -> None:
    if count > MAX_ENTRIES:
        raise PayloadTooLargeError(
            f"archive lists {count} entries; the cap is {MAX_ENTRIES}",
            limit_entries=MAX_ENTRIES,
        )


def _declared_size(info: zipfile.ZipInfo) -> int:
    return info.file_size


def _check_archive(infos: list[zipfile.ZipInfo]) -> None:
    """Refuse an archive by what its directory declares, before anything is inflated or written."""
    _refuse_entry_count(len(infos))
    declared = sum(_declared_size(i) for i in infos)
    if declared > MAX_UNCOMPRESSED_BYTES:
        raise PayloadTooLargeError(
            f"archive inflates to {declared} bytes; the cap is {MAX_UNCOMPRESSED_BYTES}",
            limit_bytes=MAX_UNCOMPRESSED_BYTES,
        )


def _read_bounded(zf: zipfile.ZipFile, info: zipfile.ZipInfo, budget: int) -> bytes:
    """Inflate one entry, refusing it the moment it passes ``budget`` bytes.

    The budget is what is left of :data:`MAX_UNCOMPRESSED_BYTES`, so this counts the bytes
    actually decompressed rather than trusting the sizes the headers declare.
    """
    out = bytearray()
    try:
        with zf.open(info) as stream:
            while chunk := stream.read(_READ_CHUNK):
                out += chunk
                if len(out) > budget:
                    raise PayloadTooLargeError(
                        f"archive inflates past the {MAX_UNCOMPRESSED_BYTES}-byte cap "
                        f"(at entry {info.filename!r})",
                        limit_bytes=MAX_UNCOMPRESSED_BYTES,
                    )
    except (zipfile.BadZipFile, EOFError, OSError, NotImplementedError) as exc:
        # A corrupt entry, a size or CRC that does not match what the headers declared, or an
        # unsupported compression method: a bad upload, not a server fault.
        raise BadRequestError(f"archive entry {info.filename!r} cannot be read: {exc}") from exc
    return bytes(out)


async def import_zip(
    tree: DocumentTreeService,
    *,
    collection_id: str,
    data: bytes | BinaryIO,
    parent: str = "",
    conflict: Literal["fail", "skip", "overwrite"] = "fail",
) -> ImportReport:
    """Import ``data`` (the archive bytes, or a seekable file holding them) into the tree.

    The archive is refused with :class:`PayloadTooLargeError` (413) when it lists more than
    :data:`MAX_ENTRIES` entries or declares more than :data:`MAX_UNCOMPRESSED_BYTES` in total,
    before anything is written; entries are then inflated through a bounded read that counts the
    bytes actually decompressed against the same cap.
    """
    source = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data
    eocd_count = _eocd_entry_count(source)
    if eocd_count is not None:
        _refuse_entry_count(eocd_count)
    try:
        zf = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise BadRequestError(f"not a zip archive: {exc}") from exc
    infos = zf.infolist()
    _check_archive(infos)
    report = ImportReport()
    inflated = 0
    for info in sorted(infos, key=lambda i: i.filename):
        if info.is_dir():
            continue
        raw = _read_bounded(zf, info, MAX_UNCOMPRESSED_BYTES - inflated)
        inflated += len(raw)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            report.rejected.append(
                {"file": info.filename, "reason": "binary or non-UTF-8 content"}
            )
            continue
        segments = [s for s in info.filename.split("/") if s]
        slugs = [slugify_segment(s) for s in segments]
        if any(s is None for s in slugs):
            report.rejected.append(
                {"file": info.filename, "reason": "path segment slugifies to empty"}
            )
            continue
        cur = parent
        for d in slugs[:-1]:
            cur = await _ensure_dir(tree, collection_id, cur, d, report)
        leaf = slugs[-1]
        path = f"{cur}/{leaf}" if cur else leaf
        try:
            await tree.create(collection_id=collection_id, parent=cur,
                              slug=leaf, body=text)
            report.created.append(path)
        except ConflictError:
            if conflict == "fail":
                raise
            if conflict == "skip":
                report.skipped.append(path)
            else:
                await tree.update(collection_id=collection_id, path=path, body=text)
                report.overwritten.append(path)
    return report


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "MAX_ENTRIES",
    "MAX_UNCOMPRESSED_BYTES",
    "ImportReport",
    "import_zip",
    "slugify_segment",
]
