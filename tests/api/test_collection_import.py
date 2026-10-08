"""POST /v1/collections/{cid}/import: zip -> document tree."""
from __future__ import annotations

import io
import zipfile

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
