"""The reserved default artifact provider cannot be set to a kind the factory cannot build (ticket 01a1226f, from the #706 review).

``artifact-storage-default`` is the row ``ArtifactStorageRegistry.get_default()`` reads, and ``build_artifact_storage`` builds only the ``db`` backend
(``filesystem`` and ``s3`` are accepted enum values whose construction raises ``ConfigError``). Only the DELETE of the reserved row was guarded, so a PUT that
switched its ``provider`` (an admin API call, or an agent with the system tools) answered 200, and then every consumer of the default failed: channel media on
the Telegram, Slack and Discord adapters, ``agent/inform.py``, the artifact serve route, the workspace routes, ``event_dispatch``, ``yield_runtime`` and
``executor_builders``. The console cannot do it (its edit form cannot change the kind).

A create was the way around (B2 of the #708 review): with the row missing (a boot whose seed failed, since the seed logs and carries on; a removal outside the
API; a restore without it) a POST stored the default with such a kind, and the boot seed, which only creates a MISSING row, never repaired it.

The check is shared (``primer/artifact/checks.py``, the D3/D5 pattern): the REST route answers 422 and the system tool a ``validation-error``, on a create as on
an update, and a default that was already switched can be switched back.
"""

from __future__ import annotations

import pytest

from primer.api.registries.artifact_storage_registry import DEFAULT_ARTIFACT_PROVIDER_ID
from primer.model.provider import ArtifactStorageProvider

COLLECTION = "/v1/artifact_storage_providers"
URL = f"{COLLECTION}/{DEFAULT_ARTIFACT_PROVIDER_ID}"
FILESYSTEM = {"id": DEFAULT_ARTIFACT_PROVIDER_ID, "provider": "filesystem", "config": {"root": "/tmp/artifacts"}}
S3 = {"id": DEFAULT_ARTIFACT_PROVIDER_ID, "provider": "s3", "config": {"bucket": "b"}}
DB = {"id": DEFAULT_ARTIFACT_PROVIDER_ID, "provider": "db", "config": {}}


def _assert_refused_as_unbuildable(r, kind: str) -> None:
    """The 422 that rest-api.md documents: the problem envelope with the stable code and the field, the message naming the refused kind.

    ``"default" in r.text`` proved nothing: the problem's ``instance`` is the row's URL, and the URL holds the id.
    """
    assert r.status_code == 422, r.text
    assert r.headers["content-type"] == "application/problem+json", r.headers
    extensions = r.json()["extensions"]
    assert extensions["error"] == "artifact_default_unbuildable", extensions
    assert extensions["field"] == "provider", extensions
    assert extensions["kind"] == "artifact_storage_provider", extensions
    assert repr(kind) in extensions["message"], extensions


async def _drop_the_default(app):
    """Remove the seeded row the way the API cannot (it refuses the DELETE): a failed seed, an edit outside the API or a restore leave it missing."""
    storage = app.state.storage_provider.get_storage(ArtifactStorageProvider)
    await storage.delete(DEFAULT_ARTIFACT_PROVIDER_ID)
    assert await storage.get(DEFAULT_ARTIFACT_PROVIDER_ID) is None
    return storage


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [FILESYSTEM, S3], ids=["filesystem", "s3"])
async def test_a_put_cannot_switch_the_default_to_a_kind_that_cannot_be_built(client, app, body) -> None:
    r = await client.put(URL, json=body)

    _assert_refused_as_unbuildable(r, body["provider"])
    assert (await client.get(URL)).json()["provider"] == "db", "the row is unchanged"
    assert await app.state.artifact_storage_registry.get_default() is not None, "and the default still resolves"


@pytest.mark.asyncio
async def test_a_put_of_the_default_that_keeps_the_db_kind_is_accepted(client) -> None:
    r = await client.put(URL, json=DB)

    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "db"


@pytest.mark.asyncio
async def test_another_row_may_still_name_a_kind_that_is_not_built_yet(client) -> None:
    """Only the reserved default is read by everything; a spare row naming ``filesystem`` breaks nothing but itself, as before."""
    created = await client.post("/v1/artifact_storage_providers", json={"id": "asp-spare", "provider": "db", "config": {}})
    assert created.status_code == 201, created.text
    try:
        r = await client.put("/v1/artifact_storage_providers/asp-spare", json={**FILESYSTEM, "id": "asp-spare"})
        assert r.status_code == 200, r.text
    finally:
        await client.delete("/v1/artifact_storage_providers/asp-spare")


@pytest.mark.asyncio
async def test_a_default_that_was_already_switched_can_be_switched_back(client, app) -> None:
    """A row broken by an earlier build (or written under the database's feet) is repaired by a PUT back to ``db``."""
    storage = app.state.storage_provider.get_storage(ArtifactStorageProvider)
    broken = ArtifactStorageProvider.model_validate(FILESYSTEM)
    await storage.update(broken)

    refused = await client.put(URL, json=S3)
    repaired = await client.put(URL, json=DB)

    _assert_refused_as_unbuildable(refused, "s3")
    assert repaired.status_code == 200, repaired.text
    assert (await client.get(URL)).json()["provider"] == "db"


# ---- a create of the reserved default (B2 of the #708 review) -----------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [FILESYSTEM, S3], ids=["filesystem", "s3"])
async def test_a_missing_default_cannot_be_created_with_a_kind_that_cannot_be_built(client, app, body) -> None:
    storage = await _drop_the_default(app)

    r = await client.post(COLLECTION, json=body)

    _assert_refused_as_unbuildable(r, body["provider"])
    assert await storage.get(DEFAULT_ARTIFACT_PROVIDER_ID) is None, "nothing is stored"


@pytest.mark.asyncio
async def test_a_missing_default_can_be_created_as_db(client, app) -> None:
    await _drop_the_default(app)

    r = await client.post(COLLECTION, json=DB)

    assert r.status_code == 201, r.text
    assert r.json()["provider"] == "db"
    assert await app.state.artifact_storage_registry.get_default() is not None, "the default resolves again"


@pytest.mark.asyncio
async def test_a_post_of_the_default_while_it_exists_is_still_a_conflict(client) -> None:
    """The router looks the id up before its pre-create hook, so an existing default answers 409 whatever kind the body names."""
    r = await client.post(COLLECTION, json=FILESYSTEM)

    assert r.status_code == 409, r.text
    assert (await client.get(URL)).json()["provider"] == "db"


@pytest.mark.asyncio
async def test_another_row_may_be_created_naming_a_kind_that_is_not_built_yet(client) -> None:
    r = await client.post(COLLECTION, json={**FILESYSTEM, "id": "asp-spare"})

    assert r.status_code == 201, r.text
    assert r.json()["provider"] == "filesystem"
