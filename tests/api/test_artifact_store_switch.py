"""The filesystem store through the API (plan 09, Task 8): the in-process twin of the Task 10 e2e journey, the preparation of a root on a create, an
edit and a draft Test, and the startup check that names every filesystem store this process cannot use.

Every root lives in the test's ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from primer.api._app_lifespan_phases import check_filesystem_artifact_stores
from primer.api.registries.artifact_storage_registry import ArtifactStorageRegistry
from primer.artifact.filesystem import ROOT_MARKER, prepare_root
from primer.model.artifact import ArtifactLocation
from primer.model.provider import ArtifactStorageProvider, SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

PROVIDERS = "/v1/artifact_storage_providers"
ACTIVE = "/v1/artifact_storage_active_config"


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _publish(client, service_id: str, files: dict[str, bytes]) -> None:
    r = await client.post(f"/v1/services/{service_id}/versions", content=_tar(files), headers={"content-type": "application/gzip"})
    assert r.status_code == 201, r.text


def _blobs(root: Path) -> list[Path]:
    return [p for p in root.rglob("artifact-*") if p.is_file() and p.suffix != ".json"]


async def test_switching_the_store_keeps_old_and_new_files_served(client, app, tmp_path: Path) -> None:
    created = await client.post("/v1/services", json={"name": "switchme", "description": "artifact store switch"})
    assert created.status_code == 201, created.text
    service_id = created.json()["id"]
    await _publish(client, service_id, {"index.html": b"<h1>one</h1>"})

    r = await client.post(PROVIDERS, json={"id": "fs-switch", "provider": "filesystem", "config": {"root": str(tmp_path / "art")}})
    assert r.status_code == 201, r.text
    assert (tmp_path / "art" / ROOT_MARKER).is_file(), "the create prepared the folder"
    r = await client.put(ACTIVE, json={"provider_id": "fs-switch"})
    assert r.status_code == 200, r.text
    assert _blobs(tmp_path / "art") == [], "the selection probe left nothing behind"

    await _publish(client, service_id, {"index.html": b"<h1>two</h1>"})

    assert any(p.read_bytes() == b"<h1>two</h1>" for p in _blobs(tmp_path / "art"))
    assert "two" in (await client.get("/svc/switchme/")).text
    versions = (await client.get(f"/v1/services/{service_id}/versions")).json()["items"]
    v1 = next(v for v in versions if v["version"] == 1)
    assert (await client.post(f"/v1/services/{service_id}/_activate", json={"version_id": v1["id"]})).status_code == 200
    assert "one" in (await client.get("/svc/switchme/")).text, "the file written before the switch is still read, from the database"

    held = await client.put(ACTIVE, json={"provider_id": "artifact-storage-default"})
    assert held.status_code == 200, held.text
    refused = await client.delete(f"{PROVIDERS}/fs-switch")
    assert refused.status_code == 409 and refused.json()["extensions"]["count"] == 1, refused.text

    assert (await client.delete(f"/v1/services/{service_id}")).status_code == 204
    assert _blobs(tmp_path / "art") == [], "deleting the service deleted its file from the store that held it"
    assert (await client.delete(f"{PROVIDERS}/fs-switch")).status_code == 204


@pytest.mark.parametrize(
    ("root", "message"),
    [("file", "is not a directory"), ("relative", "must be an absolute path")],
)
async def test_creating_a_filesystem_provider_on_an_unusable_root_is_a_422(client, app, tmp_path: Path, root: str, message: str) -> None:
    (tmp_path / "f").write_text("x")
    value = str(tmp_path / "f") if root == "file" else "relative/artifacts"

    r = await client.post(PROVIDERS, json={"id": "fs-bad", "provider": "filesystem", "config": {"root": value}})

    ext = r.json()["extensions"]
    assert r.status_code == 422 and (ext["error"], ext["field"], ext["kind"]) == ("artifact_root_unusable", "config.root", "artifact_storage_provider"), r.text
    assert message in ext["message"] and repr(value) in ext["message"]
    assert await app.state.storage_provider.get_storage(ArtifactStorageProvider).get("fs-bad") is None, "nothing is stored"


async def test_a_root_with_a_nul_character_is_a_named_422_on_create_and_edit(client, app, tmp_path: Path) -> None:
    """Review of #890, item 1: the NUL reached ``os.mkdir``, whose bare ``ValueError`` was a 500."""
    bad = str(tmp_path / "x\x00y")

    created = await client.post(PROVIDERS, json={"id": "fs-nul", "provider": "filesystem", "config": {"root": bad}})
    assert created.status_code == 422, created.text
    assert (created.json()["extensions"]["error"], created.json()["extensions"]["field"]) == ("artifact_root_unusable", "config.root")
    assert "must not contain a NUL character" in created.json()["extensions"]["message"]

    r = await client.post(PROVIDERS, json={"id": "fs-ok", "provider": "filesystem", "config": {"root": str(tmp_path / "ok")}})
    assert r.status_code == 201, r.text
    edited = await client.put(f"{PROVIDERS}/fs-ok", json={"id": "fs-ok", "provider": "filesystem", "config": {"root": bad}})
    assert edited.status_code == 422 and edited.json()["extensions"]["error"] == "artifact_root_unusable", edited.text

    draft = await client.post(f"{PROVIDERS}/_test", json={"provider": "filesystem", "config": {"root": bad}})
    assert (draft.json()["ok"], draft.json()["step"]) == (False, "prepare") and "NUL character" in draft.json()["error"], draft.text


async def test_an_edit_to_an_unusable_root_is_a_422_and_keeps_the_row(client, app, tmp_path: Path) -> None:
    r = await client.post(PROVIDERS, json={"id": "fs-edit", "provider": "filesystem", "config": {"root": str(tmp_path / "a")}})
    assert r.status_code == 201, r.text
    (tmp_path / "f").write_text("x")

    r = await client.put(f"{PROVIDERS}/fs-edit", json={"id": "fs-edit", "provider": "filesystem", "config": {"root": str(tmp_path / "f")}})

    assert r.status_code == 422 and r.json()["extensions"]["error"] == "artifact_root_unusable", r.text
    stored = await app.state.storage_provider.get_storage(ArtifactStorageProvider).get("fs-edit")
    assert stored.config.root == str(tmp_path / "a")


async def test_a_refused_move_prepares_nothing(client, app, tmp_path: Path) -> None:
    """The preparation runs LAST: a move that the holds-artifacts check refuses never creates the new root."""
    await app.state.storage_provider.get_storage(ArtifactStorageProvider).create(
        ArtifactStorageProvider(id="fs-held", provider="filesystem", config={"root": str(tmp_path / "a")}),
    )
    await app.state.storage_provider.get_storage(ArtifactLocation).create(ArtifactLocation(id="artifact-" + "1" * 32, provider_id="fs-held"))

    r = await client.put(f"{PROVIDERS}/fs-held", json={"id": "fs-held", "provider": "filesystem", "config": {"root": str(tmp_path / "b")}})

    assert r.status_code == 409 and r.json()["extensions"]["error"] == "artifact_provider_holds_artifacts", r.text
    assert not (tmp_path / "b").exists()


async def test_an_edit_prepares_a_new_root(client, tmp_path: Path) -> None:
    r = await client.post(PROVIDERS, json={"id": "fs-move", "provider": "filesystem", "config": {"root": str(tmp_path / "a")}})
    assert r.status_code == 201, r.text

    r = await client.put(f"{PROVIDERS}/fs-move", json={"id": "fs-move", "provider": "filesystem", "config": {"root": str(tmp_path / "b")}})

    assert r.status_code == 200, r.text
    assert (tmp_path / "b" / ROOT_MARKER).is_file()


async def test_a_draft_test_prepares_the_folder(client, tmp_path: Path) -> None:
    r = await client.post(f"{PROVIDERS}/_test", json={"provider": "filesystem", "config": {"root": str(tmp_path / "draft")}})

    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
    assert (tmp_path / "draft" / ROOT_MARKER).is_file()
    assert _blobs(tmp_path / "draft") == [], "the probe object is gone"


async def test_a_draft_test_on_an_unusable_root_names_prepare(client, tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")

    r = await client.post(f"{PROVIDERS}/_test", json={"provider": "filesystem", "config": {"root": str(tmp_path / "f")}})

    body = r.json()
    assert r.status_code == 200 and (body["ok"], body["step"]) == (False, "prepare"), body
    assert "is not a directory" in body["error"]


async def test_a_saved_row_test_does_not_prepare_the_folder(client, app, tmp_path: Path) -> None:
    """The saved-row Test checks what every process will find: a root with no marker fails at ``initialize`` and is NOT prepared by the check."""
    bare = tmp_path / "bare"
    bare.mkdir()
    await app.state.storage_provider.get_storage(ArtifactStorageProvider).create(
        ArtifactStorageProvider(id="fs-bare", provider="filesystem", config={"root": str(bare)}),
    )

    r = await client.post(f"{PROVIDERS}/fs-bare/_test")

    body = r.json()
    assert (body["ok"], body["step"]) == (False, "initialize"), body
    assert ROOT_MARKER in body["error"]
    assert list(bare.iterdir()) == []


async def test_selecting_a_store_whose_root_lost_its_marker_is_refused(client, tmp_path: Path) -> None:
    r = await client.post(PROVIDERS, json={"id": "fs-lost", "provider": "filesystem", "config": {"root": str(tmp_path / "a")}})
    assert r.status_code == 201, r.text
    (tmp_path / "a" / ROOT_MARKER).unlink()

    r = await client.put(ACTIVE, json={"provider_id": "fs-lost"})

    ext = r.json()["extensions"]
    assert r.status_code == 422 and (ext["error"], ext["step"]) == ("artifact_store_unusable", "initialize"), r.text
    assert (await client.get(ACTIVE)).json()["provider_id"] == "artifact-storage-default"


# ---- the startup check (ruling 2: it never raises and never holds the boot) --------------------------------------------------------------------


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "boot.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


async def test_the_startup_check_names_every_unusable_filesystem_store(sp, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Review Focus 1: a pod that lacks the shared volume says so at boot, naming each store and its root."""
    storage = sp.get_storage(ArtifactStorageProvider)
    prepare_root(str(tmp_path / "ok"))
    (tmp_path / "bare").mkdir()
    for row in (
        ArtifactStorageProvider(id="fs-ok", provider="filesystem", config={"root": str(tmp_path / "ok")}),
        ArtifactStorageProvider(id="fs-bare", provider="filesystem", config={"root": str(tmp_path / "bare")}),
        ArtifactStorageProvider(id="fs-missing", provider="filesystem", config={"root": str(tmp_path / "missing")}),
        ArtifactStorageProvider(id="db-x", provider="db"),
    ):
        await storage.create(row)
    registry = ArtifactStorageRegistry(storage=storage, storage_provider=sp)

    with caplog.at_level(logging.ERROR, logger="primer.api._app_lifespan_phases"):
        unusable = await check_filesystem_artifact_stores(registry, storage)

    assert sorted(unusable) == ["fs-bare", "fs-missing"]
    for store_id, root in (("fs-bare", "bare"), ("fs-missing", "missing")):
        line = next(r.getMessage() for r in caplog.records if repr(store_id) in r.getMessage())
        assert str(tmp_path / root) in line and "is not usable in this process" in line, line
    assert "fs-ok" not in caplog.text and "db-x" not in caplog.text
    assert list((tmp_path / "bare").iterdir()) == [] and not (tmp_path / "missing").exists(), "the check writes nothing"
    await registry.aclose()


class _Rows:
    def __init__(self, rows=(), *, fail: bool = False) -> None:
        self._rows, self._fail = list(rows), fail

    async def list(self, page, **kwargs):
        if self._fail:
            raise RuntimeError("database is down")
        return SimpleNamespace(items=self._rows, total=len(self._rows))


async def test_the_startup_check_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    class Exploding:
        async def get_provider(self, provider_id: str):
            raise RuntimeError(f"boom {provider_id}")

    row = ArtifactStorageProvider(id="fs-x", provider="filesystem", config={"root": "/nonexistent/primer-artifacts"})

    with caplog.at_level(logging.ERROR):
        assert await check_filesystem_artifact_stores(Exploding(), _Rows(fail=True)) == []   # type: ignore[arg-type]
        assert await check_filesystem_artifact_stores(Exploding(), _Rows([row])) == ["fs-x"]   # type: ignore[arg-type]

    assert "database is down" in caplog.text and "boom fs-x" in caplog.text


async def test_a_store_that_does_not_answer_is_reported_after_the_timeout(caplog: pytest.LogCaptureFixture) -> None:
    """A root on a mount that hangs must not keep the check from the stores after it."""
    reached: list[str] = []

    class Hanging:
        async def get_provider(self, provider_id: str):
            reached.append(provider_id)
            if provider_id == "fs-hang":
                await asyncio.Event().wait()

    rows = [
        ArtifactStorageProvider(id="fs-hang", provider="filesystem", config={"root": "/nonexistent/hang"}),
        ArtifactStorageProvider(id="fs-next", provider="filesystem", config={"root": "/nonexistent/next"}),
    ]

    with caplog.at_level(logging.ERROR):
        unusable = await asyncio.wait_for(check_filesystem_artifact_stores(Hanging(), _Rows(rows), timeout=0.05), 5)  # type: ignore[arg-type]

    assert unusable == ["fs-hang"] and reached == ["fs-hang", "fs-next"]
    assert "did not answer within" in caplog.text


def test_the_lifespan_runs_the_check_in_the_background() -> None:
    """Ruling 2: the boot never waits for the check. It is started as a task, after the invalidation bus is bound (a store it caches is evicted by
    a later edit in any process), and cancelled at shutdown."""
    src = (Path(__file__).resolve().parents[2] / "primer" / "api" / "_app_lifespan.py").read_text(encoding="utf-8")

    started = re.search(r"asyncio\.create_task\(\s*check_filesystem_artifact_stores\(", src)
    assert started is not None
    assert "await check_filesystem_artifact_stores(" not in src
    assert src.index("artifact_storage_registry.bind_invalidation_bus(") < started.start()
    assert "app.state.artifact_store_check_task = asyncio.create_task(" in src and "store_check.cancel()" in src


async def test_a_put_to_the_filesystem_store_records_its_provider(client, app, tmp_path: Path) -> None:
    """The filesystem store needs a record (``needs_location_record``): a put through the routing store writes one naming the provider."""
    r = await client.post(PROVIDERS, json={"id": "fs-rec", "provider": "filesystem", "config": {"root": str(tmp_path / "rec")}})
    assert r.status_code == 201, r.text
    assert (await client.put(ACTIVE, json={"provider_id": "fs-rec"})).status_code == 200
    routing = await app.state.artifact_storage_registry.get_default()

    aid = await routing.put(data=b"x", mime_type="text/plain", filename="x.txt")

    record = await app.state.storage_provider.get_storage(ArtifactLocation).get(aid)
    assert record is not None and record.provider_id == "fs-rec"
    assert (await routing.get(aid)).data == b"x"
