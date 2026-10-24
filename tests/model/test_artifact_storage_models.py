"""The models the artifact-storage routing is built on (plan 09, Task 1).

* ``ActiveArtifactStorageConfig``: the singleton that names the provider NEW artifacts go to.
* ``ArtifactLocation``: the per-artifact record of which provider holds the bytes.
* ``S3ArtifactConfig``: the ``addressing_style`` option, and a blank optional box read as unset (ticket T3: the console sends ``""`` for
  an empty box, and a ``""`` endpoint would otherwise reach botocore and read as a moved origin when the stored keys are restored).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.artifact import ArtifactLocation
from primer.model.providers.artifact import (
    ACTIVE_ARTIFACT_STORAGE_CONFIG_ID,
    DEFAULT_ARTIFACT_PROVIDER_ID,
    ActiveArtifactStorageConfig,
    S3ArtifactConfig,
)


def test_blank_optional_s3_fields_read_as_unset():
    c = S3ArtifactConfig(bucket="b", endpoint_url="", region="  ", access_key="", secret_key="")
    assert (c.endpoint_url, c.region, c.access_key, c.secret_key) == (None, None, None, None)
    assert c.prefix == ""


def test_the_key_prefix_is_not_a_blank_optional_field():
    """A prefix is a plain string with a real empty value, and whitespace around one is the operator's to keep: only the four optional connection fields are
    read as unset."""
    assert S3ArtifactConfig(bucket="b", prefix="").prefix == ""
    assert S3ArtifactConfig(bucket="b", prefix="  ").prefix == "  "


def test_a_value_that_is_set_is_kept_as_given():
    c = S3ArtifactConfig(bucket="b", endpoint_url="https://s3.example.com", region="eu-west-1", access_key="AKIA1", secret_key="s3cret")
    assert (c.endpoint_url, c.region) == ("https://s3.example.com", "eu-west-1")
    assert (c.access_key.get_secret_value(), c.secret_key.get_secret_value()) == ("AKIA1", "s3cret")


def test_a_stored_s3_row_without_addressing_style_loads_as_auto():
    assert S3ArtifactConfig.model_validate({"bucket": "b", "prefix": "p/"}).addressing_style == "auto"


@pytest.mark.parametrize("style", ["auto", "path", "virtual"])
def test_addressing_style_takes_the_three_values(style):
    assert S3ArtifactConfig(bucket="b", addressing_style=style).addressing_style == style


def test_addressing_style_takes_only_three_values():
    with pytest.raises(ValidationError):
        S3ArtifactConfig(bucket="b", addressing_style="dns")


def test_addressing_style_sits_between_region_and_the_keys():
    """The ``_types`` form lists the config fields in the model's order (tests/api/test_artifact_storage_types.py), so the position is part of the contract."""
    assert list(S3ArtifactConfig.model_fields) == [
        "bucket", "prefix", "endpoint_url", "region", "addressing_style", "access_key", "secret_key",
    ]


def test_the_active_store_defaults_to_the_reserved_database_row():
    assert ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID).provider_id == "artifact-storage-default"
    with pytest.raises(ValidationError):
        ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id="")


def test_the_active_store_is_a_singleton_with_an_explicit_id():
    """No id prefix: the row is addressed by one well-known id, never minted."""
    assert ACTIVE_ARTIFACT_STORAGE_CONFIG_ID == "_active_artifact_storage_config"
    with pytest.raises(ValueError):
        ActiveArtifactStorageConfig()


def test_a_location_record_needs_the_artifact_id():
    with pytest.raises(ValueError):
        ArtifactLocation(provider_id="fs-1")
    assert ArtifactLocation(id="artifact-" + "a" * 32, provider_id="fs-1").id == "artifact-" + "a" * 32


def test_a_location_record_needs_a_provider():
    with pytest.raises(ValidationError):
        ArtifactLocation(id="artifact-" + "a" * 32, provider_id="")
    with pytest.raises(ValidationError):
        ArtifactLocation(id="artifact-" + "a" * 32)


def test_the_default_provider_id_is_one_constant():
    from primer.api.registries import artifact_storage_registry as reg
    from primer.artifact import factory

    assert factory.DEFAULT_ARTIFACT_PROVIDER_ID is DEFAULT_ARTIFACT_PROVIDER_ID is reg.DEFAULT_ARTIFACT_PROVIDER_ID
    assert DEFAULT_ARTIFACT_PROVIDER_ID == "artifact-storage-default"


def test_the_filesystem_config_describes_a_shared_absolute_root() -> None:
    """Review of #890, item 10: the schema an agent or an API client reads said "v1 stub" and "Directory under which artifact bytes are written"."""
    from primer.model.providers.artifact import FilesystemArtifactConfig

    description = FilesystemArtifactConfig.model_fields["root"].description
    assert "absolute path" in description.lower() and "every API and worker process" in description, description
    assert "stub" not in (FilesystemArtifactConfig.__doc__ or "")
