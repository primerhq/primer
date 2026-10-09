"""``GET /v1/artifact_storage_providers/_types`` serves the form metadata of the artifact-storage class (board ticket 01a1214c, found by the #668 round-2 a11y sweep).

The provider classes with a console form serve it from ``/{plural}/_types`` (docs/dev/architecture/provider-pattern.md; ``channel_providers`` and ``workspace_providers`` have their own panels and serve none), and the console carries no field table of its own. The artifact-storage router had only
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

# ---- round 2 (lead's review of #706) ---------------------------------------------------------------------------------------------------------------------------------------


def _row_of(kind: str):
    from primer.model.provider import ArtifactStorageProvider

    config = {f["key"]: "value" for f in CONFIG_FIELDS[kind] if f["required"]}
    return ArtifactStorageProvider.model_validate({"id": f"asp-factory-{kind}", "provider": kind, "config": config})


CONFIG_FIELDS = {
    "db": [],
    "filesystem": [{"key": "root", "required": True}],
    "s3": [{"key": "bucket", "required": True}],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(CONFIG_OF))
async def test_a_kind_the_factory_cannot_build_says_so_in_its_label_and_one_it_can_does_not(client, kind: str) -> None:
    """B1: the menu must not offer as working a backend that is stored and never used. Every consumer reads only the reserved default row, and ``build_artifact_storage`` builds only ``db``; so the label of
    every kind the factory refuses says 'not implemented yet: stored, never used', and when a backend ships the label has to lose it (this test flips by itself)."""
    from primer.artifact.factory import build_artifact_storage
    from primer.model.except_ import ConfigError

    label = (await _types(client))[kind]["label"]
    try:
        build_artifact_storage(_row_of(kind), storage_provider=object())  # type: ignore[arg-type]
        builds = True
    except ConfigError:
        builds = False
    assert ("not implemented" in label) is (not builds), (kind, label, "builds" if builds else "is refused by the factory")
    if not builds:
        assert "stored, never used" in label, label


@pytest.mark.asyncio
async def test_the_database_kind_is_not_called_the_default_and_says_which_row_is_used(client) -> None:
    """A newly registered ``db`` row is not the default: only the built-in ``artifact-storage-default`` row is read by the consumers."""
    label = (await _types(client))["db"]["label"]
    assert "(default)" not in label
    assert "artifact-storage-default" in label and "only" in label.lower(), label


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "restricted"])
async def test_a_non_admin_cannot_read_the_types(raw_client, app, role: str) -> None:
    """N1: system configuration is admin only, like every sibling class (the helper router is mounted with the admin gate, not the user gate)."""
    from tests.api.test_require_user_admin import _login, _seed

    await _seed(app, uid=f"u-{role}", username=role, role=role)
    await _login(raw_client, role)
    r = await raw_client.get("/v1/artifact_storage_providers/_types")
    assert r.status_code == 403, r.text
    assert r.json()["extensions"]["error"] == "forbidden_role"


@pytest.mark.asyncio
async def test_an_anonymous_caller_cannot_read_the_types(raw_client) -> None:
    r = await raw_client.get("/v1/artifact_storage_providers/_types")
    assert r.status_code == 401, r.text
    assert r.json()["extensions"]["error"] == "auth_required"


@pytest.mark.asyncio
async def test_an_admin_reads_the_types_through_the_same_gate(raw_client, app) -> None:
    from tests.api.test_require_user_admin import _login, _seed

    await _seed(app, uid="u-admin", username="admin1", role="admin")
    await _login(raw_client, "admin1")
    assert (await raw_client.get("/v1/artifact_storage_providers/_types")).status_code == 200


def test_the_route_reuses_the_shared_field_descriptor() -> None:
    """N3: one descriptor builder for the provider classes (``providers._form_field``), not a copy per router."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "primer" / "api" / "routers" / "artifact_storage.py").read_text(encoding="utf-8")
    assert "_form_field" in src and "def _field(" not in src
