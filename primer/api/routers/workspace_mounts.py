"""Mount / import a Collection into a Workspace and detach it again.

Import uses the SIMPLE direct approach (``service.list`` + ``service.read``
+ ``ws.write_file(join_dest(...))``) rather than ``resolve_file_sources`` —
that resolver-based path is for create-time expansion (Task 6), not this
running-workspace import path.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from primer.api.deps import (
    get_collection_storage,
    get_document_service,
    get_workspace_registry,
)
from primer.api.errors import common_responses
from primer.api.routers.workspaces import MountRequest
from primer.model.collection import Collection
from primer.model.except_ import ConflictError, NotFoundError
from primer.workspace import mount_manifest as mm
from primer.workspace import mount_sync
from primer.workspace.collection_expand import build_base_snapshot, join_dest, sanitize_dest

mounts_router = APIRouter(tags=["workspace-mounts"])


async def _collection_or_404(collections, collection_id: str) -> Collection:
    c = await collections.get(collection_id)
    if c is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")
    return c


@mounts_router.post(
    "/workspaces/{workspace_id}/mounts",
    response_model=mm.MountEntry,
    status_code=201,
    responses=common_responses(404, 409, 422, 500),
    summary="Mount a collection into a running workspace",
)
async def create_mount(
    workspace_id: str,
    body: MountRequest,
    registry=Depends(get_workspace_registry),
    service=Depends(get_document_service),
    collections=Depends(get_collection_storage),
) -> mm.MountEntry:
    ws = await registry.get_workspace(workspace_id)
    coll = await _collection_or_404(collections, body.collection_id)
    manifest = await mm.load_manifest(ws)
    if mm.find_by_collection(manifest, body.collection_id) is not None:
        raise ConflictError(f"Collection {body.collection_id!r} is already mounted")
    # A Collection has no human name field -- only ``id`` (short, e.g.
    # "agent-engineering") and a long ``description``. Use the id, never the
    # description, for the dir name and the manifest's collection_name.
    dest = sanitize_dest(body.dest or coll.id)
    if mm.find_by_dest(manifest, dest) is not None:
        raise ConflictError(f"A mount already uses dest {dest!r}")
    # dest must not already exist as real files. A missing dest surfaces as
    # a not-found: the real backends (local/sandbox) raise primer's
    # NotFoundError (a PrimerError, NOT builtin FileNotFoundError) when the
    # path doesn't exist, so BOTH must be caught here or a fresh mount 404s
    # on every real backend. A returned non-empty list means the path is
    # taken -> 409; the ConflictError raised inside the try is NOT a
    # NotFoundError, so it propagates rather than being swallowed.
    try:
        existing = await ws.list_files(dest)
        if existing:
            raise ConflictError(f"Path {dest!r} already exists in the workspace")
    except (FileNotFoundError, NotFoundError):
        pass

    entries = await service.list(collection_id=body.collection_id)
    # Always create the dest dir explicitly (rather than relying on
    # write_file to imply it) so it registers as a real directory entry —
    # the root-level GET /files/tree decorates a mount root by matching
    # each dir entry's path against the manifest, which requires dest to
    # actually show up as a "dir" kind entry even when it's populated with
    # files in the same call.
    await ws.make_dir(dest)
    # From here on the dest dir exists on disk. If anything below fails
    # (a document read, a file write, or the manifest save), roll the dir
    # back so a retry doesn't trip the "dest already exists" guard above and
    # so we never leave an orphan directory with no manifest entry behind.
    try:
        if not entries:
            await ws.write_file(f"{dest}/.gitkeep", b"")
        else:
            for e in entries:
                res = await service.read(collection_id=body.collection_id, path=e.path)
                await ws.write_file(join_dest(dest, e.path), res.content.encode("utf-8"))

        base = await build_base_snapshot(service, body.collection_id)
        entry = mm.MountEntry(
            mount_id=f"wsmnt-{uuid.uuid4().hex[:12]}",
            collection_id=body.collection_id,
            collection_name=coll.id,
            dest=dest,
            mounted_at=datetime.now(timezone.utc),
            base=base,
        )
        await mm.save_manifest(ws, mm.add_mount(manifest, entry))
    except Exception:
        # Best-effort rollback: never let a cleanup failure mask the real
        # mount error (that original exception must be what the client sees).
        try:
            await ws.delete_file(dest, recursive=True)
        except Exception:
            pass
        raise
    return entry


@mounts_router.get(
    "/workspaces/{workspace_id}/mounts",
    responses=common_responses(404, 500),
    summary="List mounted collections",
)
async def list_mounts(
    workspace_id: str,
    registry=Depends(get_workspace_registry),
) -> dict:
    ws = await registry.get_workspace(workspace_id)
    manifest = await mm.load_manifest(ws)
    # Each entry gets a `dirty` flag (local copy diverged from its base
    # snapshot) so the Studio sidebar can render an unsynced-changes dot
    # without a second round-trip. Cheap enough for the small collections
    # this feature targets; a manifest with many mounts would want this
    # batched, but that's not this feature's scale.
    out = []
    for e in manifest.mounts:
        local = await mount_sync.gather_local(ws, e.dest)
        d = e.model_dump(mode="json")
        d["dirty"] = mount_sync.is_modified(e, local)
        out.append(d)
    return {"mounts": out}


@mounts_router.delete(
    "/workspaces/{workspace_id}/mounts/{mount_id}",
    status_code=204,
    responses=common_responses(404, 409, 500),
    summary="Detach a mounted collection (upstream untouched)",
)
async def delete_mount(
    workspace_id: str,
    mount_id: str,
    force: bool = Query(False),
    registry=Depends(get_workspace_registry),
) -> Response:
    ws = await registry.get_workspace(workspace_id)
    manifest = await mm.load_manifest(ws)
    entry = mm.find_mount(manifest, mount_id)
    if entry is None:
        raise NotFoundError(f"Mount {mount_id!r} does not exist")
    if not force:
        local = await mount_sync.gather_local(ws, entry.dest)
        if mount_sync.is_modified(entry, local):
            base = {b.path: b.sha256 for b in entry.base}
            changed = sorted(
                set(base) ^ set(local)
                | {p for p in local if local.get(p) != base.get(p)}
            )
            # ConflictError carries only a message string; the UI needs the
            # structured {modified, changed} payload, so raise HTTPException
            # directly here (matches the codebase's existing convention for
            # structured 4xx bodies, e.g. HTTPException(403, detail={...})).
            raise HTTPException(
                status_code=409,
                detail={"modified": True, "changed": changed},
            )
    try:
        await ws.delete_file(entry.dest, recursive=True)
    except (FileNotFoundError, NotFoundError):
        # dest was already removed out-of-band (Studio "Delete folder" / an
        # agent DELETE /files) -- the manifest entry is now stale; still
        # clean it up rather than leaving an orphaned record behind.
        pass
    await mm.save_manifest(ws, mm.remove_mount(manifest, mount_id))
    return Response(status_code=204)


@mounts_router.get(
    "/workspaces/{workspace_id}/mounts/{mount_id}/diff",
    response_model=mount_sync.DiffResult,
    responses=common_responses(404, 500),
    summary="Preview local vs collection changes",
)
async def diff_mount(
    workspace_id: str,
    mount_id: str,
    registry=Depends(get_workspace_registry),
    service=Depends(get_document_service),
    collections=Depends(get_collection_storage),
) -> mount_sync.DiffResult:
    ws = await registry.get_workspace(workspace_id)
    manifest = await mm.load_manifest(ws)
    entry = mm.find_mount(manifest, mount_id)
    if entry is None:
        raise NotFoundError(f"Mount {mount_id!r} does not exist")
    base = {b.path: b.sha256 for b in entry.base}
    local = await mount_sync.gather_local(ws, entry.dest)
    if await collections.get(entry.collection_id) is None:
        d = mount_sync.classify(base, local, {})
        d.orphaned = True
        return d
    upstream = await mount_sync.gather_upstream(service, entry.collection_id)
    return mount_sync.classify(base, local, upstream)


@mounts_router.post(
    "/workspaces/{workspace_id}/mounts/{mount_id}/apply",
    response_model=mount_sync.ApplyResult,
    responses=common_responses(404, 409, 500),
    summary="Apply local changes back to the collection",
)
async def apply_mount(
    workspace_id: str,
    mount_id: str,
    registry=Depends(get_workspace_registry),
    service=Depends(get_document_service),
    collections=Depends(get_collection_storage),
) -> mount_sync.ApplyResult:
    ws = await registry.get_workspace(workspace_id)
    manifest = await mm.load_manifest(ws)
    entry = mm.find_mount(manifest, mount_id)
    if entry is None:
        raise NotFoundError(f"Mount {mount_id!r} does not exist")
    if await collections.get(entry.collection_id) is None:
        raise ConflictError("Upstream collection no longer exists")
    base = {b.path: b.sha256 for b in entry.base}
    local = await mount_sync.gather_local(ws, entry.dest)
    upstream = await mount_sync.gather_upstream(service, entry.collection_id)
    diff = mount_sync.classify(base, local, upstream)
    result = await mount_sync.apply_changes(service, entry.collection_id, ws, entry.dest, diff)
    # Refresh base from the LOCAL disk snapshot (NOT upstream) so that a
    # file untouched locally but edited upstream since mount doesn't get its
    # base silently rewritten to the new upstream hash -- that would make a
    # later diff report it as "modified" and a later apply overwrite the
    # upstream edit with stale local content (data loss). Paths that failed
    # to apply keep their OLD base entry so they stay retryable.
    local_after = await mount_sync.gather_local(ws, entry.dest)
    old_base = {b.path: b.sha256 for b in entry.base}
    failed = set(result.failures)
    new_base = []
    seen = set()
    for path, sha in local_after.items():
        if path in failed:
            continue  # keep old base for failed paths (handled below)
        new_base.append(mm.BaseFile(path=path, sha256=sha))
        seen.add(path)
    for path, sha in old_base.items():
        if path in failed and path not in seen:
            new_base.append(mm.BaseFile(path=path, sha256=sha))
    updated = entry.model_copy(update={"base": new_base})
    manifest.mounts = [updated if e.mount_id == mount_id else e for e in manifest.mounts]
    await mm.save_manifest(ws, manifest)
    return result


__all__ = ["mounts_router"]
