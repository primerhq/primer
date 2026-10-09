"""``GET /v1/artifact_storage_providers/_types`` serves the form metadata of the artifact-storage class (board ticket 01a1214c, found by the #668 round-2 a11y sweep).

Every other provider class serves its form from ``/{plural}/_types`` (docs/dev/architecture/provider-pattern.md), and the console carries no field table of its own. The artifact-storage router had only
the CRUD routes, so the console's Register provider menu on Providers > Artifact storage said "No kinds available." and no artifact-storage provider could be registered from the page. These
tests hold the served shape to the pydantic models it describes (``primer/model/providers/artifact.py``), the way the model-family tests hold theirs to the enums, and prove that a form built from it
creates a row.
"""

from __future__ import annotations

from typing import Any

import pytest

from primer.model.providers.artifact import (
    DbArtifactConfig,
    FilesystemArtifactConfig,
    S3ArtifactConfig,
    ArtifactStorageProviderType,
)

CONFIG_OF = {
    ArtifactStorageProviderType.DB.value: DbArtifactConfig,
    ArtifactStorageProviderType.FILESYSTEM.value: FilesystemArtifactConfig,
    ArtifactStorageProviderType.S3.value: S3ArtifactConfig,
}


async def _types(client) -> dict[str, dict[str, Any]]:
    r = await client.get("/v1/artifact_storage_providers/_types")
    assert r.status_code == 200, r.text
    return r.json()


def _keys(fields: list[Any]) -> list[str]:
    return [f["key"] if isinstance(f, dict) else f for f in fields]


@pytest.mark.asyncio
async def test_the_types_cover_every_backend_and_only_those(client) -> None:
    assert set(await _types(client)) == {t.value for t in ArtifactStorageProviderType}


@pytest.mark.asyncio
async def test_each_type_has_a_label_a_person_can_read(client) -> None:
    for kind, meta in (await _types(client)).items():
        label = meta.get("label")
        assert isinstance(label, str) and len(label.split()) >= 1 and label != kind, (kind, meta)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(CONFIG_OF))
async def test_the_config_fields_are_the_fields_of_the_config_model(client, kind: str) -> None:
    """A field the model has but the form lacks cannot be set; one the form has but the model lacks is refused (``extra="forbid"``). Required and secret fields are told as the model tells them."""
    meta = (await _types(client))[kind]
    model = CONFIG_OF[kind]
    assert _keys(meta["config_fields"]) == list(model.model_fields), kind
    for field in meta["config_fields"]:
        spec = model.model_fields[field["key"]]
        assert field["required"] is spec.is_required(), (kind, field["key"])
        secret = "SecretStr" in str(spec.annotation)
        assert (field["type"] == "password") is secret, (kind, field["key"], "a secret is a password box, nothing else is")


@pytest.mark.asyncio
async def test_the_row_has_no_fields_of_its_own_no_limits_and_no_model_probe(client) -> None:
    """An artifact-storage row is ``id``, ``provider`` and ``config``: no model list, no limits block, nothing to discover."""
    for kind, meta in (await _types(client)).items():
        assert meta["row_fields"] == [], kind
        assert not meta.get("limits"), kind
        assert meta["discoverable"] is False, kind


@pytest.mark.asyncio
async def test_the_endpoint_of_an_s3_store_is_a_url_and_its_region_and_prefix_are_text(client) -> None:
    fields = {f["key"]: f for f in (await _types(client))["s3"]["config_fields"]}
    assert fields["endpoint_url"]["type"] == "url" and fields["endpoint_url"]["required"] is False
    assert fields["bucket"]["required"] is True
    assert fields["prefix"]["type"] == "text" and fields["region"]["type"] == "text"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(CONFIG_OF))
async def test_a_row_built_from_the_served_form_is_accepted(client, kind: str) -> None:
    """What the console posts: ``provider`` is the kind, ``config`` holds the REQUIRED fields it was told to ask for. If the served form asked for too little the create would come back 422, and for too much
    the model would refuse a field it does not have."""
    meta = (await _types(client))[kind]
    config = {f["key"]: ("https://objects.example" if f["type"] == "url" else "value") for f in meta["config_fields"] if f["required"]}
    r = await client.post("/v1/artifact_storage_providers", json={"id": f"asp-types-{kind}", "provider": kind, "config": config})
    assert r.status_code == 201, r.text
    try:
        assert r.json()["provider"] == kind
    finally:
        await client.delete(f"/v1/artifact_storage_providers/asp-types-{kind}")


@pytest.mark.asyncio
async def test_the_literal_types_path_beats_the_crud_get_by_id(client) -> None:
    """Mounted before the CRUD router, so "_types" is never read as an id (and is not a 404 "ArtifactStorageProvider '_types' does not exist")."""
    assert (await client.get("/v1/artifact_storage_providers/_types")).status_code == 200
    assert (await client.get("/v1/artifact_storage_providers/_nope")).status_code == 404
