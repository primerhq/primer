"""POST /v1/collections/{cid}/import: zip -> document tree."""
from __future__ import annotations

import io
import zipfile

from primer.int.vector_store import VectorStore
from primer.model.collection import Collection


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


async def _mk_collection(client) -> str:
    r = await client.post("/v1/collections", json={"description": "wiki"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_import_creates_the_tree(client):
    cid = await _mk_collection(client)
    data = _zip({"Guides/Intro.md": b"# hi", "Guides/deep/One.md": b"1"})
    r = await client.post(
        f"/v1/collections/{cid}/import",
        files={"file": ("kb.zip", data, "application/zip")},
    )
    assert r.status_code == 200, r.text
    assert sorted(r.json()["created"]) == [
        "guides", "guides/deep", "guides/deep/one", "guides/intro",
    ]

    read = await client.get(
        f"/v1/collections/{cid}/docs", params={"path": "guides/intro"},
    )
    assert read.status_code == 200
    assert read.json()["body"] == "# hi"


async def test_import_reports_binary_entries(client):
    cid = await _mk_collection(client)
    data = _zip({"ok.md": b"fine", "logo.png": b"\x89PNG\x00\x01"})
    r = await client.post(
        f"/v1/collections/{cid}/import",
        files={"file": ("kb.zip", data, "application/zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == ["ok"]
    assert body["rejected"][0]["file"] == "logo.png"


async def test_import_into_system_collection_is_403(client, fake_storage_provider):
    # A system collection is written straight to storage, as the platform does: the API refuses to create one.
    await fake_storage_provider.get_storage(Collection).create(
        Collection(id="collection-sys-import", description="sys", system=True),
    )
    data = _zip({"a.md": b"x"})
    resp = await client.post(
        "/v1/collections/collection-sys-import/import",
        files={"file": ("kb.zip", data, "application/zip")},
    )
    assert resp.status_code == 403


# ---- FS-04: the archive caps answer 413 problem+json ------------------------


async def test_an_archive_over_the_upload_cap_is_413(client, monkeypatch):
    from primer.knowledge import importer

    monkeypatch.setattr(importer, "MAX_ARCHIVE_BYTES", 64)
    cid = await _mk_collection(client)
    data = _zip({"a.md": b"x" * 200})
    assert len(data) > 64
    r = await client.post(
        f"/v1/collections/{cid}/import",
        files={"file": ("kb.zip", data, "application/zip")},
    )
    assert r.status_code == 413, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == "/errors/payload-too-large"
    assert r.json()["extensions"]["limit_bytes"] == 64


async def test_an_archive_with_too_many_entries_is_413(client, monkeypatch):
    from primer.knowledge import importer

    monkeypatch.setattr(importer, "MAX_ENTRIES", 2)
    cid = await _mk_collection(client)
    data = _zip({"a.md": b"1", "b.md": b"2", "c.md": b"3"})
    r = await client.post(
        f"/v1/collections/{cid}/import",
        files={"file": ("kb.zip", data, "application/zip")},
    )
    assert r.status_code == 413, r.text
    assert r.json()["type"] == "/errors/payload-too-large"
    read = await client.get(f"/v1/collections/{cid}/docs", params={"path": "a"})
    assert read.status_code == 404


async def test_an_archive_that_inflates_past_the_cap_is_413(client, monkeypatch):
    from primer.knowledge import importer

    monkeypatch.setattr(importer, "MAX_UNCOMPRESSED_BYTES", 1000)
    cid = await _mk_collection(client)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("bomb.md", b"a" * 100_000)
    r = await client.post(
        f"/v1/collections/{cid}/import",
        files={"file": ("kb.zip", buf.getvalue(), "application/zip")},
    )
    assert r.status_code == 413, r.text
    assert r.json()["extensions"]["limit_bytes"] == 1000


async def test_a_fail_conflict_answers_409_naming_what_was_written(client):
    """KN-19b: the 409 carries the report of what the import wrote before it stopped."""
    cid = await _mk_collection(client)
    await client.post(f"/v1/collections/{cid}/docs", json={"parent": "", "slug": "b", "body": "old"})
    r = await client.post(
        f"/v1/collections/{cid}/import",
        files={"file": ("kb.zip", _zip({"a.md": b"A", "b.md": b"B"}), "application/zip")},
    )
    assert r.status_code == 409, r.text
    assert r.json()["extensions"]["report"]["created"] == ["a"]


async def test_an_encrypted_entry_answers_400(client):
    cid = await _mk_collection(client)
    raw = bytearray(_zip({"a.md": b"hello"}))
    raw[6] |= 1
    raw[raw.rfind(b"PK\x01\x02") + 8] |= 1
    r = await client.post(f"/v1/collections/{cid}/import", files={"file": ("kb.zip", bytes(raw), "application/zip")})
    assert r.status_code == 400, r.text


# ---- 3.9: the import's indexing is one batched pass, not one call per entry ----


async def test_import_indexes_the_collection_in_one_batched_pass(client, app, fake_storage_provider):
    """After the writes, the route runs the same batched index pass that enabling search runs:
    one probe plus one embed call carrying every entry's chunks (the per-entry indexer made one
    probe plus one embed per document)."""
    from unittest.mock import AsyncMock

    from primer.model.collection import (
        CollectionEmbedder,
        CollectionSearchConfig,
    )

    # A search-enabled collection written straight to storage, as enable-search rows look.
    await fake_storage_provider.get_storage(Collection).create(Collection(
        id="kb-import",
        description="wiki",
        search=CollectionSearchConfig(
            embedder=CollectionEmbedder(provider_id="emb-1", model="m-1"),
            vector_store_provider_id="ssp-1",
        ),
    ))

    embed_calls: list[int] = []

    class _Emb:
        async def embed(self, *, model, inputs, config=None):
            embed_calls.append(len(inputs))

            class _R:
                embeddings = [type("V", (), {"vector": [0.1, 0.2]})() for _ in inputs]

            return _R()

    class _Store(VectorStore):
        async def create_collection(self, cid, *, dimensions, distance="cosine"):
            return None

        async def put_many(self, records):
            return None

        async def delete(self, cid, did):
            return None

        async def get(self, cid, did):
            return []

        async def search_by_meta(self, cid, meta):
            return []

        async def put(self, record):
            return None

        async def search(self, cid, vector, k):
            return []

        async def count_documents(self, cid):
            return 0

        async def drop_collection(self, cid):
            return None

    app.state.provider_registry.get_embedder = AsyncMock(return_value=_Emb())
    app.state.semantic_search_registry.get_store = AsyncMock(return_value=_Store())

    data = _zip({"a.md": b"A body", "b.md": b"B body", "c.md": b"C body"})
    r = await client.post(
        "/v1/collections/kb-import/import",
        files={"file": ("kb.zip", data, "application/zip")},
    )
    assert r.status_code == 200, r.text
    assert sorted(r.json()["created"]) == ["a", "b", "c"]
    assert r.json()["index_failed"] is False
    # One probe + one batched call carrying all three chunks; the per-entry
    # indexer made four calls (the probe is cached, one embed per document).
    assert embed_calls == [1, 3]


async def test_a_failed_index_pass_sets_index_failed_on_the_report(client, monkeypatch):
    """The index pass is best-effort (the rows stay written), but the caller must be able to
    tell it failed: the report carries the flag instead of the failure staying in the log."""
    from primer.knowledge import indexing

    async def _boom(**kwargs):
        raise RuntimeError("the indexer is down")

    monkeypatch.setattr(indexing, "index_documents", _boom)

    cid = await _mk_collection(client)
    r = await client.post(
        f"/v1/collections/{cid}/import",
        files={"file": ("in.zip", _zip({"doc.txt": b"hello"}), "application/zip")},
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["created"] == ["doc"]
    assert report["index_failed"] is True
