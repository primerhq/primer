"""Bulk import: zip directory structure -> document tree."""
from __future__ import annotations

import io
import re
import zipfile
from typing import Literal

from pydantic import BaseModel, Field

from primer.knowledge.tree import DocumentTreeService
from primer.model.except_ import BadRequestError, ConflictError, NotFoundError

_STRIP_EXT = re.compile(r"\.(md|markdown|txt|text)$", re.IGNORECASE)
_NON_SLUG = re.compile(r"[^a-z0-9-]+")


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


async def import_zip(
    tree: DocumentTreeService,
    *,
    collection_id: str,
    data: bytes,
    parent: str = "",
    conflict: Literal["fail", "skip", "overwrite"] = "fail",
) -> ImportReport:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BadRequestError(f"not a zip archive: {exc}") from exc
    report = ImportReport()
    for info in sorted(zf.infolist(), key=lambda i: i.filename):
        if info.is_dir():
            continue
        raw = zf.read(info)
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


__all__ = ["ImportReport", "import_zip", "slugify_segment"]
