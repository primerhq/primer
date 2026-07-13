"""The `.state/mounts.json` sidecar: which collections are mounted where.

One entry per mounted collection. ``base`` is the content-hash snapshot at
mount / last successful apply; it drives the "modified?" delete-check and the
3-way sync diff. Read/written through the live Workspace file API so it is
backend-agnostic and travels with the workspace.
"""
from __future__ import annotations

import logging
from datetime import datetime

from pydantic import BaseModel, Field

_log = logging.getLogger(__name__)

MANIFEST_PATH = ".state/mounts.json"


class BaseFile(BaseModel):
    path: str
    sha256: str


class MountEntry(BaseModel):
    mount_id: str
    collection_id: str
    collection_name: str
    dest: str
    mounted_at: datetime
    base: list[BaseFile] = Field(default_factory=list)


class MountManifest(BaseModel):
    version: int = 1
    mounts: list[MountEntry] = Field(default_factory=list)


async def load_manifest(ws) -> MountManifest:
    try:
        raw = await ws.read_file(MANIFEST_PATH)
    except FileNotFoundError:
        return MountManifest()
    except Exception as exc:  # backend-specific "missing" errors vary
        _log.warning("mounts.json unreadable (%s); treating as empty", exc)
        return MountManifest()
    try:
        return MountManifest.model_validate_json(raw)
    except Exception as exc:
        _log.warning("mounts.json malformed (%s); treating as empty", exc)
        return MountManifest()


async def save_manifest(ws, manifest: MountManifest) -> None:
    payload = manifest.model_dump_json(indent=2).encode("utf-8")
    # MANIFEST_PATH lives under the reserved ``.state`` tree, which the public
    # ``write_file`` refuses to mutate. Persist through the privileged
    # ``write_state_file`` (sibling of ``append_state_line``) so the sidecar
    # can actually be written on the real local/sandbox backends -- the public
    # path only ever worked against permissive in-memory test fakes.
    await ws.write_state_file(MANIFEST_PATH, payload)


def find_by_collection(m: MountManifest, collection_id: str) -> MountEntry | None:
    return next((e for e in m.mounts if e.collection_id == collection_id), None)


def find_by_dest(m: MountManifest, dest: str) -> MountEntry | None:
    return next((e for e in m.mounts if e.dest == dest), None)


def find_mount(m: MountManifest, mount_id: str) -> MountEntry | None:
    return next((e for e in m.mounts if e.mount_id == mount_id), None)


def add_mount(m: MountManifest, entry: MountEntry) -> MountManifest:
    return MountManifest(version=m.version, mounts=[*m.mounts, entry])


def remove_mount(m: MountManifest, mount_id: str) -> MountManifest:
    return MountManifest(
        version=m.version,
        mounts=[e for e in m.mounts if e.mount_id != mount_id],
    )
