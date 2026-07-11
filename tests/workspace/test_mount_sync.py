import hashlib
import pytest
from primer.model.except_ import NotFoundError
from primer.workspace.mount_sync import classify, is_modified, apply_changes, gather_local, DiffResult
from primer.workspace.mount_manifest import MountEntry, BaseFile
from datetime import datetime, timezone


def h(s): return hashlib.sha256(s.encode()).hexdigest()


def test_classify_added_modified_deleted_unchanged():
    base = {"keep.md": h("k"), "mod.md": h("v1"), "gone.md": h("g")}
    local = {"keep.md": h("k"), "mod.md": h("v2"), "new.md": h("n")}  # gone deleted, new added
    upstream = {"keep.md": h("k"), "mod.md": h("v1"), "gone.md": h("g")}
    d = classify(base, local, upstream)
    assert d.added == ["new.md"]
    assert d.modified == ["mod.md"]
    assert d.deleted == ["gone.md"]
    assert d.conflicts == []


def test_classify_conflict_when_both_changed():
    base = {"f.md": h("b")}
    local = {"f.md": h("local")}          # user changed
    upstream = {"f.md": h("upstream")}    # upstream also changed
    d = classify(base, local, upstream)
    assert d.modified == ["f.md"] and d.conflicts == ["f.md"]


def test_classify_upstream_only_change_is_not_a_conflict_and_not_pushed():
    base = {"f.md": h("b")}
    local = {"f.md": h("b")}              # user untouched
    upstream = {"f.md": h("upstream")}    # upstream moved
    d = classify(base, local, upstream)
    assert d.modified == [] and d.conflicts == []


def test_classify_local_modified_upstream_deleted_is_conflict():
    base = {"f.md": h("b")}
    local = {"f.md": h("local")}          # user changed
    upstream = {}                         # upstream deleted since mount
    d = classify(base, local, upstream)
    assert d.modified == ["f.md"] and d.conflicts == ["f.md"]


def test_is_modified():
    e = MountEntry(mount_id="m", collection_id="c", collection_name="C", dest="d",
                   mounted_at=datetime.now(timezone.utc), base=[BaseFile(path="f.md", sha256=h("b"))])
    assert is_modified(e, {"f.md": h("b")}) is False
    assert is_modified(e, {"f.md": h("changed")}) is True
    assert is_modified(e, {}) is True  # local deletion counts as modified


class FakeDoc:
    def __init__(self): self.upserts = []; self.deletes = []
    async def upsert(self, *, collection_id, path, content, title=None, meta=None):
        self.upserts.append((path, content))
    async def delete(self, *, collection_id, path):
        self.deletes.append(path)


class FakeWS:
    def __init__(self, files): self.files = files  # {abs_path: bytes}
    async def read_file(self, path): return self.files[path]


class _RaisingListWS:
    """A fake workspace whose list_files raises for a missing dest."""
    def __init__(self, exc): self._exc = exc
    async def list_files(self, path, *, recursive=False): raise self._exc


@pytest.mark.asyncio
async def test_gather_local_tolerates_not_found_error():
    ws = _RaisingListWS(NotFoundError("nope"))
    out = await gather_local(ws, "dest")
    assert out == {}


@pytest.mark.asyncio
async def test_gather_local_tolerates_file_not_found_error():
    ws = _RaisingListWS(FileNotFoundError("nope"))
    out = await gather_local(ws, "dest")
    assert out == {}


@pytest.mark.asyncio
async def test_apply_pushes_local_and_deletes():
    ws = FakeWS({"d/new.md": b"N", "d/mod.md": b"M"})
    diff = DiffResult(added=["new.md"], modified=["mod.md"], deleted=["gone.md"], conflicts=[])
    svc = FakeDoc()
    res = await apply_changes(svc, "c", ws, "d", diff)
    assert sorted(svc.upserts) == [("mod.md", "M"), ("new.md", "N")]
    assert svc.deletes == ["gone.md"]
    assert res.applied == {"added": 1, "modified": 1, "deleted": 1}


@pytest.mark.asyncio
async def test_apply_counts_conflicts_overwritten():
    ws = FakeWS({"d/mod.md": b"M"})
    diff = DiffResult(modified=["mod.md"], conflicts=["mod.md"])
    svc = FakeDoc()
    res = await apply_changes(svc, "c", ws, "d", diff)
    assert svc.upserts == [("mod.md", "M")]
    assert res.applied == {"added": 0, "modified": 1, "deleted": 0}
    assert res.conflicts_overwritten == 1


class FailingDoc(FakeDoc):
    async def upsert(self, *, collection_id, path, content, title=None, meta=None):
        if path == "bad.md":
            raise RuntimeError("boom")
        await super().upsert(collection_id=collection_id, path=path, content=content)


@pytest.mark.asyncio
async def test_apply_per_path_failure_is_recorded_not_fatal():
    ws = FakeWS({"d/bad.md": b"B", "d/good.md": b"G"})
    diff = DiffResult(added=["bad.md", "good.md"])
    svc = FailingDoc()
    res = await apply_changes(svc, "c", ws, "d", diff)
    assert svc.upserts == [("good.md", "G")]  # good still applied
    assert res.applied == {"added": 1, "modified": 0, "deleted": 0}  # counts only successes
    assert res.failures == ["bad.md"]
