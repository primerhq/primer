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


async def test_an_edit_that_sets_the_addressing_style_keeps_the_keys(client, app):
    """Review Focus 1 for this class: a field the edit does not touch round-trips unchanged, the new option included."""
    assert (await client.post(COLLECTION, json=_body("s3-path", endpoint_url="https://minio.example.com", region="us-east-1"))).status_code == 201
    served = (await client.get(f"{COLLECTION}/s3-path")).json()
    served["config"]["addressing_style"] = "path"

    r = await client.put(f"{COLLECTION}/s3-path", json=served)

    assert r.status_code == 200, r.text
    stored = await _stored(app, "s3-path")
    assert stored.config.addressing_style == "path"
    assert (stored.config.endpoint_url, stored.config.region, stored.config.bucket) == ("https://minio.example.com", "us-east-1", "media")
    assert stored.config.access_key.get_secret_value() == "AKIAEXAMPLEKEY1"
    assert stored.config.secret_key.get_secret_value() == "secret-example-1"


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
