"""3-way sync between a mounted collection dir and its upstream Collection.

classify() is pure over {base, local, upstream} content-hash maps keyed by
document path (dest-relative). apply_changes() pushes local -> upstream
(local wins on conflict) via DocumentService, deletes last.
"""
from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from primer.workspace.mount_manifest import MountEntry


class DiffResult(BaseModel):
    added: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    orphaned: bool = False


class ApplyResult(BaseModel):
    applied: dict[str, int]
    conflicts_overwritten: int = 0
    failures: list[str] = Field(default_factory=list)


def classify(base, local, upstream) -> DiffResult:
    d = DiffResult()
    paths = set(base) | {p for p, v in local.items() if v is not None}
    for p in sorted(paths):
        b = base.get(p)
        l = local.get(p)
        u = upstream.get(p)
        if l is None:                      # user deleted locally
            if b is not None:
                d.deleted.append(p)
            continue
        if b is None:                      # not in base -> user added
            d.added.append(p)
        elif l != b:                       # user modified
            d.modified.append(p)
        else:                              # unchanged locally -> never pushed
            continue
        if u != b:                         # upstream also moved (or deleted) since mount
            d.conflicts.append(p)
    return d


def is_modified(entry: MountEntry, local) -> bool:
    base = {bf.path: bf.sha256 for bf in entry.base}
    if set(base) != {p for p in local}:
        return True
    return any(local.get(p) != h for p, h in base.items())


async def gather_local(ws, dest) -> dict[str, str]:
    out: dict[str, str] = {}
    entries = await ws.list_files(dest, recursive=True)
    for e in entries:
        if getattr(e, "kind", None) != "file":  # skip dir / symlink / anything non-regular
            continue
        rel = e.path[len(dest) + 1:] if e.path.startswith(dest + "/") else e.path
        if rel == ".gitkeep":
            continue
        raw = await ws.read_file(e.path)
        out[rel] = hashlib.sha256(raw).hexdigest()
    return out


async def gather_upstream(service, collection_id) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in await service.list(collection_id=collection_id):
        res = await service.read(collection_id=collection_id, path=e.path)
        out[e.path] = hashlib.sha256(res.content.encode("utf-8")).hexdigest()
    return out


async def apply_changes(service, collection_id, ws, dest, diff) -> ApplyResult:
    applied = {"added": 0, "modified": 0, "deleted": 0}
    failures: list[str] = []
    conflicts = set(diff.conflicts)
    overwritten = 0
    for p in [*diff.added, *diff.modified]:
        try:
            raw = await ws.read_file(f"{dest}/{p}")
            await service.upsert(collection_id=collection_id, path=p, content=raw.decode("utf-8"))
            applied["added" if p in diff.added else "modified"] += 1
            if p in conflicts:
                overwritten += 1
        except Exception:
            failures.append(p)
    for p in diff.deleted:  # deletes last
        try:
            await service.delete(collection_id=collection_id, path=p)
            applied["deleted"] += 1
        except Exception:
            failures.append(p)
    return ApplyResult(applied=applied, conflicts_overwritten=overwritten, failures=failures)
