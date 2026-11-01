"""publish_version concurrency and cleanup (KN-19j).

Two concurrent publishes compute the same next version number from the
same read. The unique (service_id, version) index makes the loser's
create a ConflictError, which is retried on the freshly read next
number; a failed create deletes the artifacts it put, so no publish
leaves orphan artifacts behind.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.model.except_ import ConflictError, ProviderError
from primer.model.provider import SqliteConfig
from primer.model.service import Service, ServiceManifest, ServiceVersion
from primer.model.storage import OffsetPage
from primer.service.bundle import ValidatedBundle
from primer.service.publish import publish_version
from primer.storage.sqlite import SqliteStorageProvider


class _Artifacts:
    """A counting artifact store: put yields once so concurrent
    publishes interleave between reading the version list and creating
    their row."""

    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self._n = 0

    async def put(self, *, data: bytes, mime_type: str, filename: str | None = None) -> str:
        await asyncio.sleep(0)
        self._n += 1
        aid = f"art-{self._n}"
        self.stored[aid] = data
        return aid

    async def get(self, artifact_id: str):
        data = self.stored.get(artifact_id)
        if data is None:
            return None
        from primer.int.artifact_storage import ArtifactBlob

        return ArtifactBlob(data=data, mime_type="application/octet-stream")

    async def delete(self, artifact_id: str) -> None:
        self.stored.pop(artifact_id, None)
        self.deleted.append(artifact_id)

    async def aclose(self) -> None:
        pass


def _service(name: str = "svc-pub") -> Service:
    return Service(id="service-test", name=name, description="publish test service")


def _bundle() -> ValidatedBundle:
    return ValidatedBundle(
        manifest=ServiceManifest(),
        files={"index.html": b"<h1>v</h1>"},
    )


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "pub.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


# ---- characterization: a single publish pins today's behaviour ------------


async def test_single_publish_creates_version_one_and_activates(sp):
    service = _service()
    await sp.get_storage(Service).create(service)
    store = _Artifacts()

    created = await publish_version(sp, store, service=service, bundle=_bundle())

    assert created.version == 1
    assert created.service_id == service.id
    (artifact_id,) = created.files.values()
    assert store.stored.get(artifact_id) == b"<h1>v</h1>"
    assert service.active_version_id == created.id
    on_disk = await sp.get_storage(Service).get(service.id)
    assert on_disk is not None and on_disk.active_version_id == created.id


async def test_second_publish_increments_the_version(sp):
    service = _service()
    await sp.get_storage(Service).create(service)
    store = _Artifacts()
    first = await publish_version(sp, store, service=service, bundle=_bundle())
    second = await publish_version(sp, store, service=service, bundle=_bundle())
    assert (first.version, second.version) == (1, 2)
    assert second.id != first.id
    # distinct artifacts per publish
    assert set(first.files.values()).isdisjoint(second.files.values())


# ---- KN-19j: concurrent publishes must not share a version number ---------


async def test_concurrent_publishes_get_distinct_version_numbers(sp):
    service = _service()
    await sp.get_storage(Service).create(service)
    store = _Artifacts()

    results = await asyncio.gather(
        publish_version(sp, store, service=service, bundle=_bundle()),
        publish_version(sp, store, service=service, bundle=_bundle()),
    )

    numbers = sorted(v.version for v in results)
    assert numbers == [1, 2]  # today both publishes create version 1
    rows = (
        await sp.get_storage(ServiceVersion).list(OffsetPage(offset=0, length=50))
    ).items
    assert len(rows) == 2
    # both publishes landed with intact files maps
    for version in results:
        for artifact_id in version.files.values():
            assert store.stored.get(artifact_id) is not None


# ---- KN-19j: a failed create must not leave orphan artifacts --------------


async def test_conflicting_create_is_retried_and_succeeds(sp, monkeypatch):
    service = _service()
    await sp.get_storage(Service).create(service)
    store = _Artifacts()
    storage = sp.get_storage(ServiceVersion)
    calls = {"n": 0}
    real_create = storage.create

    async def flaky_create(entity):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConflictError("simulated race: version number taken")
        return await real_create(entity)

    monkeypatch.setattr(storage, "create", flaky_create)
    created = await publish_version(sp, store, service=service, bundle=_bundle())

    assert created.version == 1
    (artifact_id,) = created.files.values()
    assert store.stored.get(artifact_id) is not None
    assert store.deleted == []


async def test_exhausted_conflicts_delete_the_put_artifacts(sp, monkeypatch):
    service = _service()
    await sp.get_storage(Service).create(service)
    store = _Artifacts()
    storage = sp.get_storage(ServiceVersion)

    async def always_conflicts(entity):
        raise ConflictError("simulated race: version number taken")

    monkeypatch.setattr(storage, "create", always_conflicts)
    with pytest.raises(ConflictError):
        await publish_version(sp, store, service=service, bundle=_bundle())

    # the put artifact was deleted; no version row exists
    assert store.stored == {}
    assert len(store.deleted) == len(_bundle().files)
    rows = (
        await sp.get_storage(ServiceVersion).list(OffsetPage(offset=0, length=50))
    ).items
    assert rows == []


async def test_non_conflict_create_failure_also_cleans_up(sp, monkeypatch):
    service = _service()
    await sp.get_storage(Service).create(service)
    store = _Artifacts()
    storage = sp.get_storage(ServiceVersion)

    async def boom(entity):
        raise ProviderError("storage down")

    monkeypatch.setattr(storage, "create", boom)
    with pytest.raises(ProviderError):
        await publish_version(sp, store, service=service, bundle=_bundle())

    assert store.stored == {}
    assert len(store.deleted) == len(_bundle().files)
