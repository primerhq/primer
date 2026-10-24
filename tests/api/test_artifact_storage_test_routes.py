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


# ---- the S3 store, live on moto's server in this process (plan 09 Task 9; slice D ruling 7) ---------------------------------------------------------

from tests.artifact.conftest import S3_BUCKET, isolated_aws_env, s3_endpoint  # noqa: E402, F401  (fixtures)


async def test_a_draft_s3_store_on_a_working_bucket_tests_ok(client, s3_endpoint, isolated_aws_env) -> None:
    body = {"provider": "s3", "config": {"bucket": S3_BUCKET, "prefix": "draft-test/", "endpoint_url": s3_endpoint,
                                         "access_key": KEY, "secret_key": SECRET}}

    response = await client.post(f"{COLLECTION}/_test", json=body)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_a_failing_s3_draft_names_the_step_and_shows_no_key(client, s3_endpoint, isolated_aws_env) -> None:
    """Ruling 7: what the operator typed comes back as a diagnostic, never as the keys."""
    body = {"provider": "s3", "config": {"bucket": "no-such-bucket", "endpoint_url": s3_endpoint, "access_key": KEY, "secret_key": SECRET}}

    response = await client.post(f"{COLLECTION}/_test", json=body)

    assert response.status_code == 200
    assert (response.json()["ok"], response.json()["step"]) == (False, "put")
    assert "NoSuchBucket" in response.json()["error"]
    assert KEY not in response.text and SECRET not in response.text


async def test_a_draft_with_one_key_is_reported_at_build_and_never_uses_ambient_credentials(
    client, s3_endpoint, isolated_aws_env, monkeypatch,
) -> None:
    """Ruling 5 on the draft Test: the server has working ambient credentials, and a draft with one key still fails at ``build``."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    body = {"provider": "s3", "config": {"bucket": S3_BUCKET, "endpoint_url": s3_endpoint, "access_key": KEY}}

    response = await client.post(f"{COLLECTION}/_test", json=body)

    assert (response.json()["ok"], response.json()["step"]) == (False, "build")
    assert "set both access_key and secret_key, or neither" in response.json()["error"]
    assert KEY not in response.text
