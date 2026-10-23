"""Admin-only draft and saved-row probes for artifact storage providers."""

import pytest

from primer.model.provider import ArtifactStorageProvider
from tests.artifact._stores import MemStore


COLLECTION = "/v1/artifact_storage_providers"
KEY = "AKIAEXAMPLEKEY1"
SECRET = "secret-example-1"


def _s3_body(provider_id: str = "s3-saved") -> dict:
    return {
        "id": provider_id,
        "provider": "s3",
        "config": {
            "bucket": "media",
            "endpoint_url": "https://objects.example",
            "access_key": KEY,
            "secret_key": SECRET,
        },
    }


async def test_a_draft_database_store_tests_ok(client) -> None:
    response = await client.post(
        f"{COLLECTION}/_test", json={"provider": "db", "config": {}},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_an_incomplete_draft_is_reported_not_refused(client) -> None:
    response = await client.post(
        f"{COLLECTION}/_test", json={"provider": "s3", "config": {}},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"].startswith("invalid draft: ")


async def test_a_saved_row_is_tested_with_its_stored_secrets(
    client, app, monkeypatch,
) -> None:
    await app.state.storage_provider.get_storage(ArtifactStorageProvider).create(
        ArtifactStorageProvider.model_validate(_s3_body()),
    )
    captured = []

    def build(row, storage_provider):
        captured.append(row)
        return MemStore()

    monkeypatch.setattr("primer.artifact.probe.build_artifact_storage", build)

    response = await client.post(f"{COLLECTION}/s3-saved/_test")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert captured[0].config.secret_key.get_secret_value() == SECRET


async def test_an_edit_draft_with_the_served_masks_uses_the_stored_secrets(
    client, app, monkeypatch,
) -> None:
    await app.state.storage_provider.get_storage(ArtifactStorageProvider).create(
        ArtifactStorageProvider.model_validate(_s3_body("s3-edit")),
    )
    served = (await client.get(f"{COLLECTION}/s3-edit")).json()
    captured = []

    def build(row, storage_provider):
        captured.append(row)
        return MemStore()

    monkeypatch.setattr("primer.artifact.probe.build_artifact_storage", build)

    response = await client.post(f"{COLLECTION}/_test", json=served)

    assert response.json() == {"ok": True}
    assert captured[0].config.access_key.get_secret_value() == KEY
    assert captured[0].config.secret_key.get_secret_value() == SECRET


async def test_an_edit_draft_that_moves_the_endpoint_does_not_carry_the_stored_secrets(
    client, app, monkeypatch,
) -> None:
    await app.state.storage_provider.get_storage(ArtifactStorageProvider).create(
        ArtifactStorageProvider.model_validate(_s3_body("s3-moved")),
    )
    served = (await client.get(f"{COLLECTION}/s3-moved")).json()
    served["config"]["endpoint_url"] = "https://elsewhere.example"
    captured = []

    def build(row, storage_provider):
        captured.append(row)
        return MemStore()

    monkeypatch.setattr("primer.artifact.probe.build_artifact_storage", build)

    response = await client.post(f"{COLLECTION}/_test", json=served)

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "re-enter the secret" in response.json()["error"]
    assert captured == []


async def test_testing_an_unknown_saved_row_is_a_404(client) -> None:
    response = await client.post(f"{COLLECTION}/missing/_test")

    assert response.status_code == 404
    assert response.json()["type"] == "/errors/not-found"


@pytest.mark.parametrize("role", ["user", "restricted"])
async def test_a_non_admin_cannot_test(raw_client, app, role: str) -> None:
    from tests.api.test_require_user_admin import _login, _seed

    await _seed(app, uid=f"probe-{role}", username=f"probe-{role}", role=role)
    await _login(raw_client, f"probe-{role}")

    draft = await raw_client.post(
        f"{COLLECTION}/_test", json={"provider": "db", "config": {}},
    )
    saved = await raw_client.post(f"{COLLECTION}/saved/_test")

    assert draft.status_code == saved.status_code == 403
    assert draft.json()["extensions"]["error"] == "forbidden_role"
