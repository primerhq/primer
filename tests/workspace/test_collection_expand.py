import hashlib
import pytest
from primer.workspace.collection_expand import (
    sanitize_dest, join_dest, expand_collection, build_base_snapshot,
)


class _Entry:
    def __init__(self, path, document_id): self.path = path; self.document_id = document_id


class _Read:
    def __init__(self, content): self.content = content


class FakeDocService:
    def __init__(self, docs):  # docs: {path: (document_id, content)}
        self.docs = docs
    async def list(self, *, collection_id, prefix=None):
        return [_Entry(p, v[0]) for p, v in self.docs.items()]
    async def read(self, *, collection_id, path):
        return _Read(self.docs[path][1])


def test_sanitize_dest():
    assert sanitize_dest("SLO Runbooks") == "slo-runbooks"
    assert sanitize_dest("../../etc") == "etc"
    assert sanitize_dest("") == "collection"


def test_join_dest_rejects_traversal():
    assert join_dest("a", "b/c.md") == "a/b/c.md"
    with pytest.raises(ValueError):
        join_dest("a", "../escape.md")
    with pytest.raises(ValueError):
        join_dest("a", "/abs.md")
    with pytest.raises(ValueError):
        join_dest("../evil", "x.md")
    with pytest.raises(ValueError):
        join_dest("/abs", "x.md")


@pytest.mark.asyncio
async def test_expand_collection_builds_document_mounts():
    svc = FakeDocService({"concepts/slo.md": ("document-1", "hi"), "x.md": ("document-2", "yo")})
    mounts = await expand_collection(svc, "collection-a", "slo")
    paths = sorted(m.path for m in mounts)
    assert paths == ["slo/concepts/slo.md", "slo/x.md"]
    src = [m for m in mounts if m.path == "slo/x.md"][0].source
    assert src.kind == "document" and src.collection_id == "collection-a" and src.document_id == "document-2"


@pytest.mark.asyncio
async def test_build_base_snapshot_hashes_bodies():
    svc = FakeDocService({"x.md": ("document-2", "yo")})
    snap = await build_base_snapshot(svc, "collection-a")
    assert snap[0].path == "x.md"
    assert snap[0].sha256 == hashlib.sha256(b"yo").hexdigest()
