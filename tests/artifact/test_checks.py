"""The shared pre-write check for artifact-storage providers (ticket 01a1226f). The REST route and the system tools both run it; this pins the rule itself."""

from __future__ import annotations

import pytest

from primer.api.registries.artifact_storage_registry import DEFAULT_ARTIFACT_PROVIDER_ID
from primer.common.entity_checks import EntityCheckError
from primer.model.provider import ArtifactStorageProvider


def _row(provider: str, *, id: str = DEFAULT_ARTIFACT_PROVIDER_ID) -> ArtifactStorageProvider:  # noqa: A002
    config = {"db": {}, "filesystem": {"root": "/tmp/a"}, "s3": {"bucket": "b"}}[provider]
    return ArtifactStorageProvider.model_validate({"id": id, "provider": provider, "config": config})


def _check(entity: ArtifactStorageProvider, existing: ArtifactStorageProvider) -> None:
    from primer.artifact.checks import check_artifact_provider_on_update

    check_artifact_provider_on_update(entity, existing)


@pytest.mark.parametrize("kind", ["filesystem", "s3"])
def test_the_default_may_not_name_a_kind_the_factory_cannot_build(kind) -> None:
    with pytest.raises(EntityCheckError) as info:
        _check(_row(kind), _row("db"))

    assert info.value.kind == "validation" and info.value.field == "provider" and info.value.code == "artifact_default_unbuildable"
    assert kind in info.value.message and DEFAULT_ARTIFACT_PROVIDER_ID in info.value.message and "db" in info.value.message


def test_the_default_may_keep_db_and_be_switched_back_to_it() -> None:
    _check(_row("db"), _row("db"))
    _check(_row("db"), _row("filesystem"))          # a row broken earlier is repaired by a write that makes it buildable


def test_any_other_row_is_unrestricted() -> None:
    _check(_row("filesystem", id="asp-spare"), _row("db", id="asp-spare"))


def test_the_buildable_set_is_the_one_the_factory_dispatches_on() -> None:
    """The guard reads the factory's own list, so the day a backend ships it is allowed without a second edit."""
    from primer.artifact.factory import BUILDABLE_KINDS, build_artifact_storage
    from primer.model.except_ import ConfigError

    for kind in ("db", "filesystem", "s3"):
        row = _row(kind)
        try:
            build_artifact_storage(row, storage_provider=None)  # type: ignore[arg-type]
            built = True
        except ConfigError:
            built = False
        assert (row.provider in BUILDABLE_KINDS) is built, kind
