"""The shared pre-write check for artifact-storage providers (ticket 01a1226f). The REST route and the system tools both run it, on a create and on an
update; this pins the rule itself, the factory list it reads, and that the check and the system toolset import on their own."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import primer
from primer.api.registries.artifact_storage_registry import DEFAULT_ARTIFACT_PROVIDER_ID
from primer.common.entity_checks import EntityCheckError
from primer.model.provider import ArtifactStorageProvider, ArtifactStorageProviderType


def _row(provider: str, *, id: str = DEFAULT_ARTIFACT_PROVIDER_ID) -> ArtifactStorageProvider:  # noqa: A002
    config = {"db": {}, "filesystem": {"root": "/tmp/a"}, "s3": {"bucket": "b"}}[provider]
    return ArtifactStorageProvider.model_validate({"id": id, "provider": provider, "config": config})


def _check(entity: ArtifactStorageProvider) -> None:
    from primer.artifact.checks import check_artifact_provider_write

    check_artifact_provider_write(entity)


@pytest.mark.parametrize("kind", ["filesystem", "s3"])
def test_the_default_may_not_name_a_kind_the_factory_cannot_build(kind) -> None:
    with pytest.raises(EntityCheckError) as info:
        _check(_row(kind))

    assert info.value.kind == "validation" and info.value.field == "provider" and info.value.code == "artifact_default_unbuildable"
    assert kind in info.value.message and DEFAULT_ARTIFACT_PROVIDER_ID in info.value.message and "db" in info.value.message


def test_the_default_may_name_db() -> None:
    """The rule reads only the row being written, not the stored one: a create of a missing default and a PUT back to ``db`` that repairs a
    default switched earlier are the same accepted write."""
    _check(_row("db"))


def test_any_other_row_is_unrestricted() -> None:
    _check(_row("filesystem", id="asp-spare"))
    _check(_row("s3", id="asp-spare"))


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


def test_each_buildable_kind_builds_its_own_backend() -> None:
    """Building SOMETHING is not enough: a ``filesystem`` row that built the DB backend would store its bytes in the database and pass the test
    above. A kind that becomes buildable must name its backend class here."""
    from primer.artifact.db import DbArtifactStorage
    from primer.artifact.factory import BUILDABLE_KINDS, build_artifact_storage

    backend_of = {ArtifactStorageProviderType.DB: DbArtifactStorage}

    assert BUILDABLE_KINDS == set(backend_of), "a kind became buildable without its backend class pinned here"
    for kind, backend in backend_of.items():
        assert type(build_artifact_storage(_row(kind.value), storage_provider=None)) is backend, kind  # type: ignore[arg-type]


# ---- the check and the system toolset import on their own (B1 of the #708 review) ----------------------------------------------------------------

_ROOT = Path(primer.__file__).resolve().parents[1]


def _in_a_fresh_interpreter(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a new interpreter on the tree this process imported, so the import order is the one ``code`` makes, not the sweep's."""
    pythonpath = os.pathsep.join(path for path in (str(_ROOT), os.environ.get("PYTHONPATH")) if path)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=_ROOT, env={**os.environ, "PYTHONPATH": pythonpath}, capture_output=True, text=True, timeout=180,
    )


@pytest.mark.parametrize("module", ["primer.artifact.checks", "primer.toolset.system"])
def test_the_module_imports_first_in_a_fresh_interpreter(module) -> None:
    """The check imported ``primer.api`` and ``primer.toolset.system`` imports the check, so a process whose first primer import was either module
    went primer.api -> primer.api.app -> primer.toolset.system (half initialised) and raised ImportError. Five test modules failed to collect when
    run alone. The sweep hides it: something imports ``primer.api.app`` before it collects them, and so does the CLI."""
    done = _in_a_fresh_interpreter(f"import {module}")

    assert done.returncode == 0, done.stderr[-3000:]


def test_the_check_loads_nothing_from_the_api_package() -> None:
    """``primer.artifact`` does not import ``primer.api``: the default id lives in ``primer/artifact/factory.py`` and the registry re-exports it."""
    done = _in_a_fresh_interpreter(
        "import sys, primer.artifact.checks; print(sorted(m for m in sys.modules if m.split('.')[:2] == ['primer', 'api']))",
    )

    assert done.returncode == 0, done.stderr[-3000:]
    assert done.stdout.strip() == "[]", done.stdout
