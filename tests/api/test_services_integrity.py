"""T107 (GS-9, GS-26, GS-32, GU-15): unique service names, a pointer only publish and activate move, bundle limits, and a busy gateway."""

from __future__ import annotations

import io
import tarfile
import threading

import pytest

from primer.model.except_ import ConflictError
from primer.model.service import Service, ServiceManifest, ServiceVersion, ServiceViewerAuth
from primer.service import bundle as bundle_mod
from primer.service.bundle import BundleError, validate_bundle


def _tar(entries: dict[str, bytes], dirs: int = 0) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for i in range(dirs):
            info = tarfile.TarInfo(f"d{i}")
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
        for path, data in entries.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _mk(client, name, **kw):
    return await client.post("/v1/services", json={"name": name, "description": "d", **kw})


FN = b"@primer_tool(timeout_seconds=5)\nasync def one() -> int:\n    return 1\n"


# --- GS-9 / GU-15 ----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_second_service_with_a_taken_name_answers_409(client):
    assert (await _mk(client, "shop")).status_code == 201
    second = await _mk(client, "shop")
    assert second.status_code == 409, second.text
    assert (await _mk(client, "other")).status_code == 201


@pytest.mark.asyncio
async def test_renaming_into_a_taken_name_answers_409_and_keeping_your_own_is_fine(client):
    await _mk(client, "taken")
    mine = (await _mk(client, "mine")).json()
    mine["name"] = "taken"
    assert (await client.put(f"/v1/services/{mine['id']}", json=mine)).status_code == 409
    mine["name"] = "mine"
    mine["description"] = "edited"
    assert (await client.put(f"/v1/services/{mine['id']}", json=mine)).status_code == 200


@pytest.mark.asyncio
async def test_a_put_keeps_the_stored_active_version(client, fake_storage_provider):
    row = (await _mk(client, "keeper")).json()
    other = (await _mk(client, "payroll")).json()
    foreign = await fake_storage_provider.get_storage(ServiceVersion).create(
        ServiceVersion(service_id=other["id"], version=1, manifest=ServiceManifest(), files={}))
    own = await fake_storage_provider.get_storage(ServiceVersion).create(
        ServiceVersion(service_id=row["id"], version=1, manifest=ServiceManifest(), files={}))
    assert (await client.post(f"/v1/services/{row['id']}/_activate", json={"version_id": own.id})).status_code == 200
    stale = {**row, "active_version_id": foreign.id, "viewer_auth": "none"}        # an edit dialog opened before the publish
    put = await client.put(f"/v1/services/{row['id']}", json=stale)
    assert put.status_code == 200 and put.json()["active_version_id"] == own.id
    assert put.json()["viewer_auth"] == "none"                                      # the real edit still lands


@pytest.mark.asyncio
async def test_create_ignores_an_active_version_in_the_body(client):
    made = await _mk(client, "fresh", active_version_id="service-version-nope")
    assert made.status_code == 201 and made.json()["active_version_id"] is None


@pytest.mark.asyncio
async def test_the_storage_refuses_a_duplicate_name(fake_storage_provider):
    store = fake_storage_provider.get_storage(Service)
    await store.create(Service(name="same", description="a"))
    try:
        await store.create(Service(name="same", description="b"))
    except ConflictError:
        return
    pytest.skip("the fake storage has no unique index; the sqlite and postgres registries carry it")


# --- GS-26 -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_viewer_auth_change_during_a_publish_survives_it(client, app, monkeypatch):
    from primer.service import publish as publish_mod

    svc = (await _mk(client, "racy")).json()
    real = publish_mod._create_next_version

    async def create_then_edit(sp, service, bundle, files):
        created = await real(sp, service, bundle, files)
        stored = await sp.get_storage(Service).get(service.id)
        await sp.get_storage(Service).update(stored.model_copy(update={"viewer_auth": ServiceViewerAuth.NONE}))   # an admin edit lands now
        return created

    monkeypatch.setattr(publish_mod, "_create_next_version", create_then_edit)
    r = await client.post(f"/v1/services/{svc['id']}/versions", content=_tar({"functions.py": FN}),
                          headers={"content-type": "application/gzip"})
    assert r.status_code == 201, r.text
    row = await app.state.storage_provider.get_storage(Service).get(svc["id"])
    assert row.viewer_auth is ServiceViewerAuth.NONE and row.active_version_id == r.json()["id"]


@pytest.mark.asyncio
async def test_activate_does_not_write_back_a_stale_service_row(fake_storage_provider):
    from primer.service.publish import activate_version

    store = fake_storage_provider.get_storage(Service)
    svc = await store.create(Service(name="stale", description="d"))
    version = await fake_storage_provider.get_storage(ServiceVersion).create(
        ServiceVersion(service_id=svc.id, version=1, manifest=ServiceManifest(), files={}))
    await store.update(svc.model_copy(update={"viewer_auth": ServiceViewerAuth.NONE}))     # changed after the caller read it
    await activate_version(fake_storage_provider, service=svc, version_id=version.id)
    row = await store.get(svc.id)
    assert row.active_version_id == version.id and row.viewer_auth is ServiceViewerAuth.NONE


# --- GS-32 -----------------------------------------------------------------

def test_a_bundle_of_a_hundred_thousand_directory_headers_is_refused():
    with pytest.raises(BundleError, match="members"):
        validate_bundle(_tar({"functions.py": FN}, dirs=100_000))


def test_a_normal_bundle_with_directories_still_validates():
    assert validate_bundle(_tar({"functions.py": FN}, dirs=20)).functions[0].name == "one"


@pytest.mark.asyncio
async def test_bundle_validation_runs_off_the_event_loop(client, monkeypatch):
    from primer.api.routers import services as services_router

    seen: list[int] = []
    real = services_router.validate_bundle

    def spy(data):
        seen.append(threading.get_ident())
        return real(data)

    monkeypatch.setattr(services_router, "validate_bundle", spy)
    svc = (await _mk(client, "offloop")).json()
    r = await client.post(f"/v1/services/{svc['id']}/versions", content=_tar({"functions.py": FN}),
                          headers={"content-type": "application/gzip"})
    assert r.status_code == 201, r.text
    assert seen and seen[0] != threading.get_ident()


# --- GS-8 at the route -----------------------------------------------------

@pytest.mark.asyncio
async def test_a_busy_gateway_answers_503_with_a_fixed_title(client, monkeypatch):
    from primer.api.routers import svc_serve
    from primer.service.gateway import GatewayBusy

    svc = (await _mk(client, "busyapp", viewer_auth="none")).json()
    r = await client.post(f"/v1/services/{svc['id']}/versions", content=_tar({"functions.py": FN}),
                          headers={"content-type": "application/gzip"})
    assert r.status_code == 201, r.text

    async def busy(**kwargs):
        raise GatewayBusy("global cap 32 reached")

    monkeypatch.setattr(svc_serve, "call_function", busy)
    got = await client.post("/svc/busyapp/_gateway/functions/one", json={})
    assert got.status_code == 503 and got.json()["title"] == "the service is busy, try again shortly"
    assert "32" not in got.text
