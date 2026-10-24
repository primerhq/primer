"""A console edit of an S3 row keeps its keys (plan 09 Review Focus 3, Task 1).

The console sends ``""`` for an empty optional box (board ticket T3) and the served masks for the two keys. ``endpoint_url`` is one of the fields that name WHERE a
stored secret is sent (``primer.model.common._ORIGIN_URL_FIELDS``), so a ``""`` against the stored ``None`` read as a moved origin and the PUT was refused with
"re-enter the secret"; and a ``""`` endpoint stored on the row would have reached botocore as an empty URL. A blank optional box now reads as unset.
"""

from __future__ import annotations

import pytest

from primer.model.provider import ArtifactStorageProvider

COLLECTION = "/v1/artifact_storage_providers"


def _body(provider_id: str, **config) -> dict:
    return {
        "id": provider_id,
        "provider": "s3",
        "config": {"bucket": "media", "access_key": "AKIAEXAMPLEKEY1", "secret_key": "secret-example-1", **config},
    }


async def _stored(app, provider_id: str) -> ArtifactStorageProvider:
    row = await app.state.storage_provider.get_storage(ArtifactStorageProvider).get(provider_id)
    assert row is not None
    return row


async def test_a_console_edit_of_an_s3_row_keeps_its_keys(client, app):
    assert (await client.post(COLLECTION, json=_body("s3-edit"))).status_code == 201
    served = (await client.get(f"{COLLECTION}/s3-edit")).json()
    served["config"].update(endpoint_url="", region="")      # what the console sends for an empty box (ticket T3)

    r = await client.put(f"{COLLECTION}/s3-edit", json=served)

    assert r.status_code == 200, r.text
    stored = await _stored(app, "s3-edit")
    assert stored.config.access_key.get_secret_value() == "AKIAEXAMPLEKEY1"
    assert stored.config.secret_key.get_secret_value() == "secret-example-1"
    assert (stored.config.endpoint_url, stored.config.region, stored.config.addressing_style) == (None, None, "auto")


async def test_the_served_row_carries_the_addressing_style(client):
    assert (await client.post(COLLECTION, json=_body("s3-style"))).status_code == 201
    served = (await client.get(f"{COLLECTION}/s3-style")).json()
    assert served["config"]["addressing_style"] == "auto"
    assert served["config"]["access_key"] == "**********", "the keys are still served masked"


async def test_an_edit_of_one_field_round_trips_every_other_field(client, app):
    """Master Review Focus 1 for this class: a row with every field set, one field edited through the console, and every other field unchanged."""
    every_field = {
        "bucket": "media", "prefix": "files/", "endpoint_url": "https://minio.example.com", "region": "us-east-1",
        "addressing_style": "virtual",
    }
    assert (await client.post(COLLECTION, json=_body("s3-all", **every_field))).status_code == 201
    served = (await client.get(f"{COLLECTION}/s3-all")).json()
    assert {k: served["config"][k] for k in every_field} == every_field
    served["config"]["prefix"] = "other/"                  # the one field the operator edits

    r = await client.put(f"{COLLECTION}/s3-all", json=served)

    assert r.status_code == 200, r.text
    stored = (await _stored(app, "s3-all")).config
    assert (stored.bucket, stored.prefix, stored.endpoint_url, stored.region, stored.addressing_style) == (
        "media", "other/", "https://minio.example.com", "us-east-1", "virtual",
    )
    assert stored.access_key.get_secret_value() == "AKIAEXAMPLEKEY1"
    assert stored.secret_key.get_secret_value() == "secret-example-1"


async def test_an_edit_that_sets_the_addressing_style_keeps_the_keys(client, app):
    assert (await client.post(COLLECTION, json=_body("s3-path", endpoint_url="https://minio.example.com", region="us-east-1"))).status_code == 201
    served = (await client.get(f"{COLLECTION}/s3-path")).json()
    served["config"]["addressing_style"] = "path"

    r = await client.put(f"{COLLECTION}/s3-path", json=served)

    assert r.status_code == 200, r.text
    stored = (await _stored(app, "s3-path")).config
    assert stored.addressing_style == "path"
    assert (stored.endpoint_url, stored.region, stored.bucket) == ("https://minio.example.com", "us-east-1", "media")
    assert stored.access_key.get_secret_value() == "AKIAEXAMPLEKEY1"
    assert stored.secret_key.get_secret_value() == "secret-example-1"


async def test_pointing_the_endpoint_elsewhere_still_refuses_the_served_masks(client):
    """The guard that makes ``endpoint_url`` an origin is untouched: a blank box is the unset endpoint, but a new host is still a moved origin."""
    assert (await client.post(COLLECTION, json=_body("s3-moved"))).status_code == 201
    served = (await client.get(f"{COLLECTION}/s3-moved")).json()
    served["config"]["endpoint_url"] = "https://attacker.example.com"

    r = await client.put(f"{COLLECTION}/s3-moved", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the secret" in r.text


async def test_a_blank_secret_on_an_edit_clears_the_stored_key(client, app):
    """A blank key box is the operator clearing it (a deliberately blanked OPTIONAL secret nulls the stored one, tests/api/test_provider_secret_masking.py),
    which is the ambient-credentials setup."""
    assert (await client.post(COLLECTION, json=_body("s3-clear"))).status_code == 201
    served = (await client.get(f"{COLLECTION}/s3-clear")).json()
    served["config"].update(access_key="", secret_key="")

    r = await client.put(f"{COLLECTION}/s3-clear", json=served)

    assert r.status_code == 200, r.text
    stored = await _stored(app, "s3-clear")
    assert (stored.config.access_key, stored.config.secret_key) == (None, None)


# ---- both keys or neither (plan 09 Task 9; slice D ruling 5) and the keys are never served (ruling 7) -----------------------------------------------
#
# With one key dropped, botocore would sign with whatever ambient credentials the server has (a pod role, an env pair), so a half pair is refused on
# every write instead of being completed from the environment.

HALF_KEY = "AKIAHALFPAIRKEY01"
HALF_SECRET = "half-pair-secret-01"


@pytest.mark.parametrize(
    ("keys", "field"),
    [({"access_key": HALF_KEY}, "config.secret_key"), ({"secret_key": HALF_SECRET}, "config.access_key"),
     ({"access_key": HALF_KEY, "secret_key": ""}, "config.secret_key")],
    ids=["access key only", "secret key only", "blank secret"],
)
async def test_a_create_with_one_key_is_refused_naming_the_other(client, app, keys: dict, field: str):
    r = await client.post(COLLECTION, json={"id": "s3-half", "provider": "s3", "config": {"bucket": "media", **keys}})

    assert r.status_code == 422, r.text
    ext = r.json()["extensions"]
    assert (ext["error"], ext["kind"], ext["field"]) == ("artifact_s3_keys_incomplete", "artifact_storage_provider", field)
    assert ext["message"] == "set both access_key and secret_key, or neither to use the server's own credentials"
    assert HALF_KEY not in r.text and HALF_SECRET not in r.text
    assert await app.state.storage_provider.get_storage(ArtifactStorageProvider).get("s3-half") is None


async def test_an_edit_that_blanks_one_key_is_refused_and_keeps_both(client, app):
    """The served mask of the access key is restored first (``preserve_masked_secrets``), then the pair is judged: a blanked secret beside a kept
    access key is a half pair."""
    assert (await client.post(COLLECTION, json=_body("s3-blank-one"))).status_code == 201
    served = (await client.get(f"{COLLECTION}/s3-blank-one")).json()
    served["config"]["secret_key"] = ""

    r = await client.put(f"{COLLECTION}/s3-blank-one", json=served)

    assert r.status_code == 422, r.text
    assert (r.json()["extensions"]["error"], r.json()["extensions"]["field"]) == ("artifact_s3_keys_incomplete", "config.secret_key")
    stored = (await _stored(app, "s3-blank-one")).config
    assert (stored.access_key.get_secret_value(), stored.secret_key.get_secret_value()) == ("AKIAEXAMPLEKEY1", "secret-example-1")


async def test_both_keys_are_served_masked_in_a_read_and_a_list(client):
    assert (await client.post(COLLECTION, json=_body("s3-masked"))).status_code == 201

    one = await client.get(f"{COLLECTION}/s3-masked")
    listed = await client.get(COLLECTION)

    assert (one.json()["config"]["access_key"], one.json()["config"]["secret_key"]) == ("**********", "**********")
    for r in (one, listed):
        assert "secret-example-1" not in r.text and "AKIAEXAMPLEKEY1" not in r.text
