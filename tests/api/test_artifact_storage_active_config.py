"""HTTP contract for the singleton active artifact-storage setting."""

from __future__ import annotations

import pytest

from primer.artifact.probe import ArtifactProbeResult

ACTIVE = "/v1/artifact_storage_active_config"


async def _create_db(client, provider_id: str = "db-2") -> None:
    response = await client.post(
        "/v1/artifact_storage_providers",
        json={"id": provider_id, "provider": "db", "config": {}},
    )
    assert response.status_code == 201, response.text


async def test_a_fresh_install_stores_new_files_in_the_database(client) -> None:
    response = await client.get(ACTIVE)
    assert response.status_code == 200
    assert response.json() == {
        "id": "_active_artifact_storage_config",
        "provider_id": "artifact-storage-default",
    }


async def test_a_put_switches_the_store(client) -> None:
    await _create_db(client)
    assert (await client.put(ACTIVE, json={"provider_id": "db-2"})).json()["provider_id"] == "db-2"
    assert (await client.get(ACTIVE)).json()["provider_id"] == "db-2"


async def test_a_put_naming_no_provider_is_a_422(client) -> None:
    response = await client.put(ACTIVE, json={"provider_id": "nope"})
    assert response.status_code == 422
    extensions = response.json()["extensions"]
    assert (extensions["error"], extensions["field"]) == ("unknown_provider_id", "provider_id")


async def test_a_store_that_fails_its_check_is_not_selected(client, monkeypatch) -> None:
    await _create_db(client)

    async def failing(*args, **kwargs):
        return ArtifactProbeResult(ok=False, step="put", error="PermissionError: denied")

    monkeypatch.setattr("primer.artifact.active.probe_artifact_provider", failing)
    response = await client.put(ACTIVE, json={"provider_id": "db-2"})

    extensions = response.json()["extensions"]
    assert response.status_code == 422
    assert extensions["error"] == "artifact_store_unusable"
    assert extensions["step"] == "put"
    assert "PermissionError: denied" in extensions["message"]
    assert (await client.get(ACTIVE)).json()["provider_id"] == "artifact-storage-default"


async def test_the_active_store_cannot_be_deleted(client) -> None:
    await _create_db(client)
    assert (await client.put(ACTIVE, json={"provider_id": "db-2"})).status_code == 200

    response = await client.delete("/v1/artifact_storage_providers/db-2")

    extensions = response.json()["extensions"]
    assert response.status_code == 409
    assert extensions["error"] == "cascade_blocked"
    assert extensions["referenced_by"] == "_active_artifact_storage_config"


@pytest.mark.parametrize("role", ["user", "restricted"])
async def test_a_non_admin_can_neither_read_nor_set_it(raw_client, app, role: str) -> None:
    from tests.api.test_require_user_admin import _login, _seed

    await _seed(app, uid=f"u-active-{role}", username=role, role=role)
    await _login(raw_client, role)
    assert (await raw_client.get(ACTIVE)).status_code == 403
    assert (await raw_client.put(ACTIVE, json={"provider_id": "db-2"})).status_code == 403


async def test_a_store_deleted_while_it_is_checked_is_a_409_and_is_not_selected(client, app, monkeypatch) -> None:
    """The G5 audit, G5-06: the selection is written only if the provider still exists after the probe."""
    from primer.model.provider import ArtifactStorageProvider

    await _create_db(client)

    async def deleted_meanwhile(row, *, storage_provider):
        await storage_provider.get_storage(ArtifactStorageProvider).delete(row.id)
        return ArtifactProbeResult(ok=True)

    monkeypatch.setattr("primer.artifact.active.probe_artifact_provider", deleted_meanwhile)
    response = await client.put(ACTIVE, json={"provider_id": "db-2"})

    assert response.status_code == 409, response.text
    extensions = response.json()["extensions"]
    assert (extensions["error"], extensions["field"]) == ("artifact_store_deleted", "provider_id")
    assert (await client.get(ACTIVE)).json()["provider_id"] == "artifact-storage-default"
