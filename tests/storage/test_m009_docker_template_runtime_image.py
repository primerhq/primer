"""Migration 9: stored Docker workspace templates name the runtime image they already ran (platform revamp 2026-10, plan 08 task 6;
design 20 Q6).

Until the revamp the Docker adapter ignored the image a template named: it always ran ``primer/workspace-runtime:1.1`` and only
recorded the typed value in a label. Once the typed image is honoured (task 5), a stored template that names ``python:3.12``
would suddenly run ``python:3.12`` and fail, since that image holds no primer workspace runtime. So every stored Docker template is
rewritten to the image it was already running, and nothing that worked breaks.

The rows are seeded through a class that reads them untyped (the table is selected by the class NAME, as in m007's and m008's
tests), so the test sees exactly the JSON the migration sees, secrets in the clear.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.workspace import DOCKER_RUNTIME_IMAGE
from primer.model.workspace import WorkspaceTemplate as LiveWorkspaceTemplate
from primer.storage.sqlite import SqliteStorageProvider

SECRET = "s3cr3t"
PASSWORD = "pw"


class WorkspaceTemplate(Identifiable):  # noqa: N801 - the class name selects the storage table
    """A row as the old schema stored it: every field beyond the id is read untyped and kept."""

    model_config = ConfigDict(extra="allow")


def _row(row_id: str, **fields: Any) -> WorkspaceTemplate:
    return WorkspaceTemplate(id=row_id, **fields)


ROWS = {
    # The case the migration exists for: a typed image the adapter never ran, beside every other kind of field a template holds.
    "docker-custom": _row(
        "docker-custom", provider_id="docker-1", description="a Docker template with every field set",
        backend={"kind": "container", "image": "python:3.12", "entrypoint": ["python"], "user": "1000:1000", "workdir": "/workspace",
                 "cpu_cores": 2, "memory_bytes": 2**31, "extra_mounts": [{"host": "/srv", "container": "/data", "readonly": True}],
                 "network": {"egress": "deny_all"}},
        env={"TOKEN": SECRET},
        files=[{"path": "seed.txt", "source": {"kind": "url", "url": f"https://u:{PASSWORD}@files.local/x", "sha256": "a" * 64}, "mode": "0644"}],
        init_commands=["echo hello"], state_path=".state", tmp_path=".tmp", strict_write_locking=True,
        resources={"cpu_cores": 4, "memory_bytes": 2**32, "network": "egress", "disk_bytes": None},
        a_future_key={"kept": [1, 2, 3]},
    ),
    "docker-empty": _row("docker-empty", provider_id="docker-1", description="d", backend={"kind": "container", "image": ""}),
    "docker-runtime": _row(
        "docker-runtime", provider_id="docker-1", description="d", backend={"kind": "container", "image": DOCKER_RUNTIME_IMAGE, "cpu_cores": 1},
    ),
    "docker-other-tag": _row("docker-other-tag", provider_id="docker-1", description="d", backend={"kind": "container", "image": "primer/workspace-runtime:1.0"}),
    "k8s": _row("k8s", provider_id="k3s", description="d", backend={"kind": "kubernetes", "image": "reg:5000/rt:1", "pvc_size": "10Gi"}),
    "local": _row("local", provider_id="local", description="d", backend={"kind": "local"}),
    # Written before templates had a backend at all: the live model defaults it to local when it loads.
    "legacy": _row("legacy", provider_id="local", description="d"),
}
MOVED = ("docker-custom", "docker-empty", "docker-other-tag")
LEFT_ALONE = ("docker-runtime", "k8s", "local", "legacy")


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[StorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "m009.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _seed(sp: StorageProvider) -> None:
    store = sp.get_storage(WorkspaceTemplate)
    for row in ROWS.values():
        await store.create(row)


async def _raw(sp: StorageProvider) -> dict[str, dict[str, Any]]:
    page = await sp.get_storage(WorkspaceTemplate).list(OffsetPage(offset=0, length=100))
    return {row.id: row.model_dump() for row in page.items}


def _without_image(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["backend"] = {k: v for k, v in row["backend"].items() if k != "image"}
    return out


def _migration():
    from primer.storage.migrations.m009_docker_template_runtime_image import M009DockerTemplateRuntimeImage

    return M009DockerTemplateRuntimeImage()


# ---- what it rewrites -------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m009_moves_docker_templates_to_the_runtime_image(sp: StorageProvider) -> None:
    await _seed(sp)
    before = await _raw(sp)

    await _migration().apply(sp)

    after = await _raw(sp)
    for row_id in MOVED:
        assert after[row_id]["backend"]["image"] == "primer/workspace-runtime:1.1", row_id
        assert _without_image(after[row_id]) == _without_image(before[row_id]), row_id     # env, files, entrypoint, cpu, network kept as stored
    for row_id in LEFT_ALONE:
        assert after[row_id] == before[row_id], row_id


@pytest.mark.asyncio
async def test_m009_keeps_every_other_key_of_a_moved_row_exactly(sp: StorageProvider) -> None:
    """The secrets stay in the clear and the url's password stays in the url: the migration reads and writes the stored JSON
    untyped, so nothing is masked, normalised or dropped (an unknown future key included)."""
    await _seed(sp)

    await _migration().apply(sp)

    moved = (await _raw(sp))["docker-custom"]
    assert moved["env"] == {"TOKEN": SECRET}
    assert moved["files"][0]["source"]["url"] == f"https://u:{PASSWORD}@files.local/x"
    assert moved["files"][0]["source"]["sha256"] == "a" * 64
    assert moved["backend"]["entrypoint"] == ["python"] and moved["backend"]["network"] == {"egress": "deny_all"}
    assert moved["backend"]["cpu_cores"] == 2 and moved["backend"]["extra_mounts"] == [{"host": "/srv", "container": "/data", "readonly": True}]
    assert moved["a_future_key"] == {"kept": [1, 2, 3]}
    assert moved["init_commands"] == ["echo hello"] and moved["strict_write_locking"] is True
    assert moved["resources"] == {"cpu_cores": 4, "memory_bytes": 2**32, "network": "egress", "disk_bytes": None}


@pytest.mark.asyncio
async def test_m009_only_touches_container_rows(sp: StorageProvider) -> None:
    """A Kubernetes template's image is the image that runs and was never ignored; it is not ours to rewrite."""
    await _seed(sp)

    await _migration().apply(sp)

    assert (await _raw(sp))["k8s"]["backend"]["image"] == "reg:5000/rt:1"


@pytest.mark.asyncio
async def test_a_row_without_a_usable_backend_is_left_alone(sp: StorageProvider) -> None:
    store = sp.get_storage(WorkspaceTemplate)
    await store.create(_row("nulled", provider_id="p", description="d", backend=None))
    await store.create(_row("not-a-dict", provider_id="p", description="d", backend="container"))
    await store.create(_row("no-kind", provider_id="p", description="d", backend={"image": "python:3.12"}))
    before = await _raw(sp)

    await _migration().apply(sp)

    assert await _raw(sp) == before


@pytest.mark.asyncio
async def test_a_database_with_no_workspace_template_is_a_no_op(sp: StorageProvider) -> None:
    await _migration().apply(sp)

    assert await _raw(sp) == {}


@pytest.mark.asyncio
async def test_m009_pages_through_every_template(sp: StorageProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read in pages (200 in production); a small page shows none is skipped at a page boundary."""
    from primer.storage.migrations import m009_docker_template_runtime_image as module

    monkeypatch.setattr(module, "_PAGE", 2)
    store = sp.get_storage(WorkspaceTemplate)
    for i in range(7):
        await store.create(_row(f"docker-{i}", provider_id="docker-1", description="d", backend={"kind": "container", "image": f"img:{i}"}))

    await _migration().apply(sp)

    assert {row_id: row["backend"]["image"] for row_id, row in (await _raw(sp)).items()} == {f"docker-{i}": DOCKER_RUNTIME_IMAGE for i in range(7)}


# ---- idempotent, and quiet about secrets -------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m009_is_idempotent_and_logs_ids_and_images_only(sp: StorageProvider, caplog: pytest.LogCaptureFixture) -> None:
    await _seed(sp)

    with caplog.at_level(logging.DEBUG, logger="primer"):
        await _migration().apply(sp)
    first = [r for r in caplog.records if r.name.startswith("primer.") and r.levelno == logging.WARNING]
    once = await _raw(sp)

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="primer"):
        await _migration().apply(sp)
    second = [r for r in caplog.records if r.name.startswith("primer.") and r.levelno >= logging.WARNING]

    assert await _raw(sp) == once, "a second run changes nothing"
    assert second == [], "and says nothing"
    assert {r.template_id: r.previous_image for r in first} == {"docker-custom": "python:3.12", "docker-empty": "", "docker-other-tag": "primer/workspace-runtime:1.0"}
    assert all(r.image == DOCKER_RUNTIME_IMAGE for r in first)
    everything = " ".join(r.getMessage() + " " + str(r.__dict__) for r in caplog.records + first)
    assert SECRET not in everything and PASSWORD not in everything and "files.local" not in everything, "the log names ids and images, never a value"


@pytest.mark.asyncio
async def test_every_row_loads_through_the_live_model_afterwards(sp: StorageProvider) -> None:
    await _seed(sp)

    await _migration().apply(sp)

    page = await sp.get_storage(LiveWorkspaceTemplate).list(OffsetPage(offset=0, length=100))
    by_id = {row.id: row for row in page.items}
    assert set(by_id) == set(ROWS)
    for row_id in MOVED:
        assert by_id[row_id].backend.image == DOCKER_RUNTIME_IMAGE
    assert by_id["docker-custom"].backend.entrypoint == ["python"]
    assert by_id["docker-custom"].env["TOKEN"].get_secret_value() == SECRET
    assert by_id["k8s"].backend.image == "reg:5000/rt:1"
    assert by_id["legacy"].backend.kind == "local"


# ---- it is registered, and the runner applies it ----------------------------------------------------------------------------------------------------------------------


def test_m009_is_registered_as_version_9() -> None:
    """Contiguous with m008. (The latest version is whatever the registry's last migration says; this pins index 8, not the end.)"""
    from primer.storage.migrations import LATEST_VERSION, MIGRATIONS
    from primer.storage.migrations.m009_docker_template_runtime_image import M009DockerTemplateRuntimeImage

    assert isinstance(MIGRATIONS[8], M009DockerTemplateRuntimeImage)
    assert MIGRATIONS[8].version == 9 and LATEST_VERSION >= 9
    assert MIGRATIONS[8].description == "Docker workspace templates name the runtime image they already ran"


@pytest.mark.asyncio
async def test_the_runner_applies_it_to_a_database_at_version_eight(sp: StorageProvider) -> None:
    from primer.storage.migrations import LATEST_VERSION, run_migrations

    await _seed(sp)
    await sp.set_schema_version(8)

    version = await run_migrations(sp, is_fresh_install=False)

    assert version == LATEST_VERSION and (await sp.get_system_state()).schema_version == LATEST_VERSION
    assert (await _raw(sp))["docker-custom"]["backend"]["image"] == DOCKER_RUNTIME_IMAGE
