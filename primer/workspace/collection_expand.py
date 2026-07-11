"""Expand a whole Collection into per-document FileMounts + a base snapshot.

Reuses the existing `_DocumentSource` FileSource so materialisation is
unchanged (one FileMount -> one file). ``build_base_snapshot`` records the
content hashes that later power the modified-check and 3-way sync diff.
"""
from __future__ import annotations

import hashlib
import re

from primer.model.workspace import FileMount, _DocumentSource
from primer.workspace.mount_manifest import BaseFile

_SAFE = re.compile(r"[^a-z0-9._-]+")


def sanitize_dest(raw: str) -> str:
    s = _SAFE.sub("-", (raw or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return s or "collection"


def join_dest(dest: str, doc_path: str) -> str:
    if dest.startswith("/") or doc_path.startswith("/") or "\\" in dest or "\\" in doc_path:
        raise ValueError(f"unsafe path {dest!r}/{doc_path!r}")
    joined = f"{dest}/{doc_path}"
    if any(part == ".." for part in joined.split("/")):
        raise ValueError(f"path {joined!r} escapes the mount dest")
    return joined


async def expand_collection(service, collection_id: str, dest: str) -> list[FileMount]:
    entries = await service.list(collection_id=collection_id)
    mounts: list[FileMount] = []
    for e in entries:
        mounts.append(
            FileMount(
                path=join_dest(dest, e.path),
                source=_DocumentSource(collection_id=collection_id, document_id=e.document_id),
            )
        )
    return mounts


async def build_base_snapshot(service, collection_id: str) -> list[BaseFile]:
    entries = await service.list(collection_id=collection_id)
    out: list[BaseFile] = []
    for e in entries:
        res = await service.read(collection_id=collection_id, path=e.path)
        digest = hashlib.sha256(res.content.encode("utf-8")).hexdigest()
        out.append(BaseFile(path=e.path, sha256=digest))
    return out
