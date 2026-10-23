"""A provider row cannot move away from artifact bytes that still depend on it."""

from __future__ import annotations

import pytest

from primer.model.artifact import ArtifactLocation
from primer.model.provider import ArtifactStorageProvider


async def _seed_held(app, tmp_path):
    providers = app.state.storage_provider.get_storage(ArtifactStorageProvider)
    locations = app.state.storage_provider.get_storage(ArtifactLocation)
    rows = {
        "fs-held": ArtifactStorageProvider(
            id="fs-held",
            provider="filesystem",
            config={"root": str(tmp_path / "a")},
        ),
        "s3-held": ArtifactStorageProvider(
            id="s3-held",
            provider="s3",
            config={"bucket": "media", "prefix": "p/"},
        ),
    }
    for row in rows.values():
        await providers.create(row)
        for index in range(3):
            await locations.create(
                ArtifactLocation(id=f"artifact-{row.id}-{index}", provider_id=row.id),
            )
    return rows


async def test_a_provider_that_holds_artifacts_cannot_be_deleted(client, app, tmp_path) -> None:
    await _seed_held(app, tmp_path)

    response = await client.delete("/v1/artifact_storage_providers/fs-held")

    extensions = response.json()["extensions"]
    assert response.status_code == 409
    assert extensions["error"] == "artifact_provider_holds_artifacts"
    assert extensions["count"] == 3
    assert "3 artifact(s)" in extensions["message"]


async def test_a_provider_that_holds_none_can_be_deleted(client, app, tmp_path) -> None:
    row = ArtifactStorageProvider(id="fs-empty", provider="filesystem", config={"root": str(tmp_path / "empty")})
    await app.state.storage_provider.get_storage(ArtifactStorageProvider).create(row)
    assert (await client.delete("/v1/artifact_storage_providers/fs-empty")).status_code == 204


@pytest.mark.parametrize(
    ("row_id", "change", "field"),
    [
        ("fs-held", "root", "config.root"),
        ("s3-held", "bucket", "config.bucket"),
        ("s3-held", "prefix", "config.prefix"),
        ("fs-held", "provider", "provider"),
    ],
)
async def test_moving_a_store_that_holds_artifacts_is_refused(
    client,
    app,
    tmp_path,
    row_id: str,
    change: str,
    field: str,
) -> None:
    rows = await _seed_held(app, tmp_path)
    body = rows[row_id].model_dump(mode="json")
    if change == "root":
        body["config"]["root"] = str(tmp_path / "b")
    elif change == "bucket":
        body["config"]["bucket"] = "other"
    elif change == "prefix":
        body["config"]["prefix"] = "q/"
    else:
        body.update(provider="db", config={})

    response = await client.put(f"/v1/artifact_storage_providers/{row_id}", json=body)

    extensions = response.json()["extensions"]
    assert response.status_code == 409
    assert extensions["error"] == "artifact_provider_holds_artifacts"
    assert extensions["field"] == field
    assert extensions["count"] == 3
    assert await app.state.storage_provider.get_storage(ArtifactStorageProvider).get(row_id) == rows[row_id]


async def test_readdressing_a_store_that_holds_artifacts_is_allowed(client, app, tmp_path) -> None:
    rows = await _seed_held(app, tmp_path)
    body = rows["s3-held"].model_dump(mode="json")
    body["config"].update(endpoint_url="http://minio.internal:9000", region="eu-west-1")

    response = await client.put("/v1/artifact_storage_providers/s3-held", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["config"]["endpoint_url"] == "http://minio.internal:9000"


async def test_moving_a_store_that_holds_nothing_is_allowed(client, app, tmp_path) -> None:
    providers = app.state.storage_provider.get_storage(ArtifactStorageProvider)
    row = ArtifactStorageProvider(id="fs-empty", provider="filesystem", config={"root": str(tmp_path / "a")})
    await providers.create(row)
    body = row.model_dump(mode="json")
    body["config"]["root"] = str(tmp_path / "b")

    response = await client.put("/v1/artifact_storage_providers/fs-empty", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["config"]["root"] == str(tmp_path / "b")
