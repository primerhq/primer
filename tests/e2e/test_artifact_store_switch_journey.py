"""End-to-end: switch the artifact store to a filesystem store, write, and read old and new files (plan 09, Task 10).

A service publishes v1 while the database is the active store, then a filesystem provider is registered (the create prepares its root and
marker), selected, and v2 is published into it. Both versions stay readable: v2 from the folder, v1 from the database. The store cannot be
deleted while it is active, nor while it still holds v2's files; once the service is gone (its files deleted through the store that holds
them) it can.

The e2e server is a host process (``scripts/e2e/bringup.sh`` runs ``uv run primer api``) run by the same user as pytest, so the test's own
``tmp_path`` serves as the root. The suite runs serially (``-n0``), so no other test writes while this one has the filesystem store selected;
the ``finally`` selects the database again whatever happens.
"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest

PROVIDERS = "/v1/artifact_storage_providers"
ACTIVE = "/v1/artifact_storage_active_config"
DEFAULT = "artifact-storage-default"


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _blobs(root: Path) -> list[Path]:
    return [p for p in root.rglob("artifact-*") if p.is_file() and p.suffix != ".json"]


@pytest.mark.asyncio
async def test_switching_the_store_keeps_old_and_new_files_readable(client, tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store_id, name = f"e2e-fs-{uuid4().hex[:8]}", f"e2e-art-{uuid4().hex[:8]}"
    service_id: str | None = None
    provider_created = False
    try:
        # 1. v1 under the database store.
        created = await client.post("/v1/services", json={"name": name, "description": "artifact store switch journey"})
        assert created.status_code == 201, created.text
        service_id = created.json()["id"]
        v1 = await client.post(
            f"/v1/services/{service_id}/versions", content=_tar({"index.html": b"<h1>one</h1>"}), headers={"content-type": "application/gzip"},
        )
        assert v1.status_code == 201, v1.text

        # 2. Register the filesystem store: the create prepares the folder and its marker.
        r = await client.post(PROVIDERS, json={"id": store_id, "provider": "filesystem", "config": {"root": str(root)}})
        assert r.status_code == 201, r.text
        provider_created = True
        assert os.path.isfile(root / ".primer-artifact-root")

        # 3. Select it for new files.
        r = await client.put(ACTIVE, json={"provider_id": store_id})
        assert r.status_code == 200, r.text
        assert (await client.get(ACTIVE)).json()["provider_id"] == store_id

        # 4. v2 goes to the folder.
        v2 = await client.post(
            f"/v1/services/{service_id}/versions",
            content=_tar({"index.html": b"<h1>two</h1>", "app.js": b"1"}),
            headers={"content-type": "application/gzip"},
        )
        assert v2.status_code == 201, v2.text
        assert any(p.read_bytes() == b"<h1>two</h1>" for p in _blobs(root)), "a blob file under the root holds v2's page"

        # 5. Both versions are served: v2 from the folder, v1 (activated again) from the database.
        page = await client.get(f"/svc/{name}/")
        assert page.status_code == 200 and "two" in page.text, page.text
        versions = (await client.get(f"/v1/services/{service_id}/versions")).json()["items"]
        v1_id = next(v["id"] for v in versions if v["version"] == 1)
        r = await client.post(f"/v1/services/{service_id}/_activate", json={"version_id": v1_id})
        assert r.status_code == 200, r.text
        page = await client.get(f"/svc/{name}/")
        assert page.status_code == 200 and "one" in page.text, page.text

        # 6. The active store cannot be deleted.
        r = await client.delete(f"{PROVIDERS}/{store_id}")
        assert r.status_code == 409 and r.json()["extensions"]["error"] == "cascade_blocked", r.text

        # 7. Nor can a store that still holds files: v2's two.
        r = await client.put(ACTIVE, json={"provider_id": DEFAULT})
        assert r.status_code == 200, r.text
        r = await client.delete(f"{PROVIDERS}/{store_id}")
        extensions = r.json()["extensions"]
        assert r.status_code == 409 and extensions["error"] == "artifact_provider_holds_artifacts", r.text
        assert extensions["count"] == 2, extensions
    finally:
        await client.put(ACTIVE, json={"provider_id": DEFAULT})
        if service_id is not None:
            # The service's files are deleted through the store that holds each of them.
            r = await client.delete(f"/v1/services/{service_id}")
            assert r.status_code == 204, r.text
        if provider_created:
            r = await client.delete(f"{PROVIDERS}/{store_id}")
            assert r.status_code == 204, r.text
            assert _blobs(root) == [], "no blob file is left under the root"
