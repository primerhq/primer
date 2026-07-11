import logging
import pytest
from datetime import datetime, timezone
from primer.workspace.mount_manifest import (
    MountManifest, MountEntry, BaseFile,
    load_manifest, save_manifest, find_by_collection, find_mount,
    find_by_dest, add_mount, remove_mount, MANIFEST_PATH,
)


class FakeWS:
    """Minimal in-memory Workspace file API for manifest tests."""
    def __init__(self, files=None):
        self.files = dict(files or {})
    async def read_file(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]
    async def write_file(self, path, content):
        assert isinstance(content, bytes)
        self.files[path] = content


def _entry(cid="collection-a", dest="a", mid="wsmnt-1"):
    return MountEntry(
        mount_id=mid, collection_id=cid, collection_name="A",
        dest=dest, mounted_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        base=[BaseFile(path="x.md", sha256="deadbeef")],
    )


@pytest.mark.asyncio
async def test_load_missing_manifest_returns_empty():
    m = await load_manifest(FakeWS())
    assert m.version == 1 and m.mounts == []


@pytest.mark.asyncio
async def test_load_malformed_manifest_returns_empty(caplog):
    caplog.set_level(logging.WARNING)
    ws = FakeWS({MANIFEST_PATH: b"{not json"})
    m = await load_manifest(ws)
    assert m.mounts == []
    assert "malformed" in caplog.text


@pytest.mark.asyncio
async def test_save_then_load_roundtrip():
    ws = FakeWS()
    await save_manifest(ws, MountManifest(mounts=[_entry()]))
    m = await load_manifest(ws)
    assert len(m.mounts) == 1 and m.mounts[0].collection_id == "collection-a"


def test_find_and_mutation_helpers():
    m = MountManifest(mounts=[_entry()])
    assert find_by_collection(m, "collection-a").mount_id == "wsmnt-1"
    assert find_by_collection(m, "collection-z") is None
    assert find_by_dest(m, "a").mount_id == "wsmnt-1"
    assert find_mount(m, "wsmnt-1").dest == "a"
    m2 = add_mount(m, _entry(cid="collection-b", dest="b", mid="wsmnt-2"))
    assert len(m2.mounts) == 2
    m3 = remove_mount(m2, "wsmnt-1")
    assert [e.mount_id for e in m3.mounts] == ["wsmnt-2"]
