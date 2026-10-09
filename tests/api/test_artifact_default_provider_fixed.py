"""The reserved default artifact provider cannot be switched to a kind the factory cannot build (ticket 01a1226f, from the #706 review).

``artifact-storage-default`` is the row ``ArtifactStorageRegistry.get_default()`` reads, and ``build_artifact_storage`` builds only the ``db`` backend
(``filesystem`` and ``s3`` are accepted enum values whose construction raises ``ConfigError``). Only the DELETE of the reserved row was guarded, so a PUT that
switched its ``provider`` (an admin API call, or an agent with the system tools) answered 200, and then every consumer of the default failed: channel media on
the Telegram, Slack and Discord adapters, ``agent/inform.py``, the artifact serve route, the workspace routes, ``event_dispatch``, ``yield_runtime`` and
``executor_builders``. The console cannot do it (its edit form cannot change the kind).

The check is shared (``primer/artifact/checks.py``, the D3/D5 pattern): the REST route answers 422 and the system tool a ``validation-error``, and a default
that was already switched can be switched back.
"""

from __future__ import annotations

import pytest

from primer.api.registries.artifact_storage_registry import DEFAULT_ARTIFACT_PROVIDER_ID
from primer.model.provider import ArtifactStorageProvider

URL = f"/v1/artifact_storage_providers/{DEFAULT_ARTIFACT_PROVIDER_ID}"
FILESYSTEM = {"id": DEFAULT_ARTIFACT_PROVIDER_ID, "provider": "filesystem", "config": {"root": "/tmp/artifacts"}}
S3 = {"id": DEFAULT_ARTIFACT_PROVIDER_ID, "provider": "s3", "config": {"bucket": "b"}}
DB = {"id": DEFAULT_ARTIFACT_PROVIDER_ID, "provider": "db", "config": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [FILESYSTEM, S3], ids=["filesystem", "s3"])
async def test_a_put_cannot_switch_the_default_to_a_kind_that_cannot_be_built(client, app, body) -> None:
    r = await client.put(URL, json=body)

    assert r.status_code == 422, r.text
    assert "default" in r.text and body["provider"] in r.text
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

    assert refused.status_code == 422, refused.text
    assert repaired.status_code == 200, repaired.text
    assert (await client.get(URL)).json()["provider"] == "db"
