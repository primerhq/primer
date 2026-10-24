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
    # The root is never touched: building a filesystem store does no IO, and nothing here prepares or initializes one.
    config = {"db": {}, "filesystem": {"root": "/nonexistent/primer-artifacts"}, "s3": {"bucket": "b"}}[provider]
    return ArtifactStorageProvider.model_validate({"id": id, "provider": provider, "config": config})


def _check(entity: ArtifactStorageProvider) -> None:
    from primer.artifact.checks import check_artifact_provider_write

    check_artifact_provider_write(entity)


@pytest.mark.parametrize("kind", ["filesystem", "s3"])
def test_the_default_must_stay_db(kind) -> None:
    with pytest.raises(EntityCheckError) as info:
        _check(_row(kind))

    assert (info.value.kind, info.value.field, info.value.code) == ("validation", "provider", "artifact_default_must_be_db")
    assert repr(kind) in info.value.message and DEFAULT_ARTIFACT_PROVIDER_ID in info.value.message


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


def test_a_new_buildable_kind_must_not_relax_the_reserved_database_rule() -> None:
    """#784 review tripwire: a new backend must extend the tests above while the reserved row remains the database fallback.

    ``filesystem`` shipped with plan 09 Task 8 and ``test_the_default_must_stay_db`` already refuses it for the reserved row, so the set pinned
    here grew by that one kind. The next backend (``s3``, Task 9) moves the tripwire again."""
    from primer.artifact.factory import BUILDABLE_KINDS

    assert BUILDABLE_KINDS == {ArtifactStorageProviderType.DB, ArtifactStorageProviderType.FILESYSTEM}, (
        "another buildable kind shipped; extend test_the_default_must_stay_db "
        "without relaxing the reserved row's database-only rule"
    )


def test_each_buildable_kind_builds_its_own_backend() -> None:
    """Building SOMETHING is not enough: a ``filesystem`` row that built the DB backend would store its bytes in the database and pass the test
    above. A kind that becomes buildable must name its backend class here."""
    from primer.artifact.db import DbArtifactStorage
    from primer.artifact.factory import BUILDABLE_KINDS, build_artifact_storage
    from primer.artifact.filesystem import FilesystemArtifactStorage

    backend_of = {ArtifactStorageProviderType.DB: DbArtifactStorage, ArtifactStorageProviderType.FILESYSTEM: FilesystemArtifactStorage}

    assert BUILDABLE_KINDS == set(backend_of), "a kind became buildable without its backend class pinned here"
    for kind, backend in backend_of.items():
        assert type(build_artifact_storage(_row(kind.value), storage_provider=None)) is backend, kind  # type: ignore[arg-type]


def test_a_built_filesystem_store_uses_the_rows_root() -> None:
    from primer.artifact.factory import build_artifact_storage

    store = build_artifact_storage(_row("filesystem", id="fs-1"), storage_provider=None)  # type: ignore[arg-type]

    assert store.root == "/nonexistent/primer-artifacts"


# ---- preparing a filesystem root on a create or an edit (plan 09 Task 8) --------------------------------------------------------------------------


async def test_prepare_maps_an_unusable_root_to_config_root(tmp_path) -> None:
    from primer.artifact.checks import prepare_artifact_provider

    (tmp_path / "f").write_text("x")
    row = ArtifactStorageProvider(id="fs-1", provider="filesystem", config={"root": str(tmp_path / "f")})

    with pytest.raises(EntityCheckError) as info:
        await prepare_artifact_provider(row)

    assert (info.value.kind, info.value.code, info.value.field) == ("validation", "artifact_root_unusable", "config.root")
    assert info.value.tool_message().startswith("config.root: artifact root ") and "is not a directory" in info.value.message


async def test_prepare_writes_the_marker_of_a_filesystem_root(tmp_path) -> None:
    from primer.artifact.checks import prepare_artifact_provider
    from primer.artifact.filesystem import ROOT_MARKER

    await prepare_artifact_provider(ArtifactStorageProvider(id="fs-1", provider="filesystem", config={"root": str(tmp_path / "r")}))

    assert (tmp_path / "r" / ROOT_MARKER).is_file()


async def test_prepare_runs_off_the_event_loop(tmp_path, monkeypatch) -> None:
    import threading

    from primer.artifact import checks

    seen: list[int] = []
    real = checks.prepare_root

    def spy(root: str) -> None:
        seen.append(threading.get_ident())
        real(root)

    monkeypatch.setattr(checks, "prepare_root", spy)

    await checks.prepare_artifact_provider(ArtifactStorageProvider(id="fs-1", provider="filesystem", config={"root": str(tmp_path / "r")}))

    assert len(seen) == 1 and seen[0] != threading.get_ident()


def test_only_a_new_filesystem_root_is_prepared() -> None:
    """Review of #890, item 5: a create, a kind change or a root change prepares; the root a stored row already names is only checked."""
    from primer.artifact.checks import filesystem_root_is_new

    fs = _row("filesystem", id="fs-1")
    moved = ArtifactStorageProvider(id="fs-1", provider="filesystem", config={"root": "/nonexistent/elsewhere"})
    db = _row("db", id="fs-1")

    assert filesystem_root_is_new(fs, None)
    assert filesystem_root_is_new(fs, db)
    assert filesystem_root_is_new(moved, fs)
    assert not filesystem_root_is_new(fs, fs)
    assert not filesystem_root_is_new(db, fs), "a row that is not a filesystem row has no root to prepare"


async def test_an_update_that_keeps_the_root_checks_it_and_never_prepares(tmp_path, monkeypatch) -> None:
    from primer.artifact import checks

    monkeypatch.setattr(checks, "prepare_root", lambda root: pytest.fail("an unchanged root is never prepared"))
    (tmp_path / "bare").mkdir()
    row = ArtifactStorageProvider(id="fs-1", provider="filesystem", config={"root": str(tmp_path / "bare")})

    with pytest.raises(EntityCheckError) as info:
        await checks.prepare_artifact_provider(row, row)

    assert (info.value.code, info.value.field) == ("artifact_root_unusable", "config.root")
    assert list((tmp_path / "bare").iterdir()) == []


@pytest.mark.parametrize("kind", ["db", "s3"])
async def test_prepare_does_nothing_for_another_kind(kind, monkeypatch) -> None:
    from primer.artifact import checks

    monkeypatch.setattr(checks, "prepare_root", lambda root: pytest.fail("only a filesystem row is prepared"))
    monkeypatch.setattr(checks, "check_root", lambda root: pytest.fail("only a filesystem row is checked"))

    await checks.prepare_artifact_provider(_row(kind, id="other"))


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
