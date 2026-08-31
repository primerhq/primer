"""POST /v1/collections/{cid}/import: zip -> document tree."""
from __future__ import annotations

import io
import zipfile


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


async def test_import_into_system_collection_is_403(client):
    r = await client.post(
        "/v1/collections",
        json={"id": "collection-sys-import", "description": "sys", "system": True},
    )
    assert r.status_code == 201, r.text
    data = _zip({"a.md": b"x"})
    resp = await client.post(
        "/v1/collections/collection-sys-import/import",
        files={"file": ("kb.zip", data, "application/zip")},
    )
    assert resp.status_code == 403
