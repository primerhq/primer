import logging

import pytest
from datetime import UTC, datetime
from pydantic import SecretStr

from primer.api.registries.workspace_registry import WorkspaceRegistry
from primer.model.common import dump_for_storage
from primer.model.except_ import NotFoundError
from primer.model.workspace import Workspace as WorkspaceRow
from primer.model.workspace import WorkspaceRuntimeMeta, WorkspaceTemplate

# A real template: materialise reads its env and sizes before it asks the backend for anything.
_TEMPLATE = WorkspaceTemplate(id="t", provider_id="prov1", description="d")


class _StubSecretProvider:
    async def get_secret(self, name):
        return SecretStr("v") if name == "known" else None


class _RecordingBackend:
    def __init__(self):
        self.received_resolvers = None

    async def create(
        self, template, *, overrides=None, workspace_id=None, resolvers=None
    ):
        self.received_resolvers = resolvers
        return "workspace-handle"


class _ReattachBackend:
    def __init__(self):
        self.template = None

    async def get(self, workspace_id, *, template=None):
        self.template = template
        return "reattached-handle"


@pytest.mark.asyncio
async def test_materialise_passes_resolvers(monkeypatch):
    backend = _RecordingBackend()
    reg = WorkspaceRegistry(
        storage_provider=object(),
        secret_provider=_StubSecretProvider(),
    )

    async def _fake_get_backend(provider_id):
        return backend

    monkeypatch.setattr(reg, "get_backend", _fake_get_backend)

    result = await reg.materialise(template=_TEMPLATE)
    assert result == "workspace-handle"
    assert backend.received_resolvers is not None
    assert backend.received_resolvers.document_resolver is not None
    assert backend.received_resolvers.secret_resolver is not None


@pytest.mark.asyncio
async def test_materialise_without_secret_provider_omits_secret_resolver(monkeypatch):
    backend = _RecordingBackend()
    reg = WorkspaceRegistry(storage_provider=object())  # no secret provider

    async def _fake_get_backend(provider_id):
        return backend

    monkeypatch.setattr(reg, "get_backend", _fake_get_backend)

    await reg.materialise(template=_TEMPLATE)
    assert backend.received_resolvers is not None
    assert backend.received_resolvers.document_resolver is not None
    assert backend.received_resolvers.secret_resolver is None


@pytest.mark.asyncio
async def test_reattach_uses_the_workspace_template_snapshot(
    fake_storage_provider, monkeypatch,
):
    snapshot = WorkspaceTemplate(
        id="t", provider_id="prov1", description="create-time recipe",
    )
    edited = snapshot.model_copy(update={"description": "later edit"})
    row = WorkspaceRow(
        id="w1",
        template_id=snapshot.id,
        provider_id=snapshot.provider_id,
        created_at=datetime.now(UTC),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://runtime.test:5959/", token=SecretStr("token"),
        ),
    ).model_copy(update={"template_snapshot": snapshot})
    await fake_storage_provider.get_storage(WorkspaceRow).create(row)
    await fake_storage_provider.get_storage(WorkspaceTemplate).create(edited)

    backend = _ReattachBackend()
    registry = WorkspaceRegistry(fake_storage_provider)

    async def _fake_get_backend(provider_id):
        return backend

    monkeypatch.setattr(registry, "get_backend", _fake_get_backend)

    assert await registry.get_workspace("w1") == "reattached-handle"
    assert backend.template is snapshot


# ---- the G5 audit, G5-08: a row written without a snapshot still re-attaches -------------------------------------------------------------
#
# Migration 12 snapshots every row once, at boot; a process still on the code before it (a worker, a second API replica, during a
# rollout) went on writing rows without one, and nothing backfilled them: re-attach got template=None and the workspace was "re-attach
# failed" for good. These run on a real SQLite storage, so the backfill is the real field-scoped patch_if.


@pytest.fixture
async def sqlite_sp(tmp_path):
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "ws.sqlite"))
    await sp.initialize()
    yield sp
    await sp.aclose()


async def _legacy_row(sp, *, template: WorkspaceTemplate | None) -> WorkspaceRow:
    """A row as a process before migration 12 writes it: no snapshot."""
    if template is not None:
        await sp.get_storage(WorkspaceTemplate).create(template)
    row = WorkspaceRow(
        id="w-old", template_id="t", provider_id="prov1", created_at=datetime.now(UTC),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://runtime.test:5959/", token=SecretStr("token")),
    )
    assert row.template_snapshot is None
    return await sp.get_storage(WorkspaceRow).create(row)


def _registry(sp, backend, monkeypatch) -> WorkspaceRegistry:
    registry = WorkspaceRegistry(sp)

    async def _fake_get_backend(provider_id):
        return backend

    monkeypatch.setattr(registry, "get_backend", _fake_get_backend)
    return registry


async def test_a_row_without_a_snapshot_reattaches_from_its_template_and_gets_the_snapshot(sqlite_sp, monkeypatch):
    template = WorkspaceTemplate(id="t", provider_id="prov1", description="the recipe", env={"TOKEN": SecretStr("s3cret")})
    await _legacy_row(sqlite_sp, template=template)
    backend = _ReattachBackend()

    assert await _registry(sqlite_sp, backend, monkeypatch).get_workspace("w-old") == "reattached-handle"

    assert backend.template is not None and backend.template.description == "the recipe"
    stored = await sqlite_sp.get_storage(WorkspaceRow).get("w-old")
    assert stored.template_snapshot is not None and stored.template_snapshot.description == "the recipe"
    assert stored.template_snapshot.env["TOKEN"].get_secret_value() == "s3cret", "the snapshot keeps the stored secret, as m012's does"

    edited = template.model_copy(update={"description": "a later edit"})
    await sqlite_sp.get_storage(WorkspaceTemplate).update(edited)
    await _registry(sqlite_sp, backend, monkeypatch).get_workspace("w-old")
    assert backend.template.description == "the recipe", "from now on the snapshot is what re-attaches, not the template row"


async def test_the_backfill_writes_only_the_snapshot_and_a_snapshot_written_meanwhile_wins(sqlite_sp, monkeypatch):
    """Field-scoped: a concurrent writer's field survives, and a snapshot someone else wrote first is the one used and kept."""
    await _legacy_row(sqlite_sp, template=WorkspaceTemplate(id="t", provider_id="prov1", description="the template row"))
    rows = sqlite_sp.get_storage(WorkspaceRow)
    templates = sqlite_sp.get_storage(WorkspaceTemplate)
    real_get = templates.get

    async def get_while_others_write(template_id):
        found = await real_get(template_id)
        await rows.patch_if("w-old", {"name": "renamed meanwhile"}, where={"name": [None]})
        other = WorkspaceTemplate(id="t", provider_id="prov1", description="the other process's snapshot")
        await rows.patch_if("w-old", {"template_snapshot": dump_for_storage(other)}, where={"template_snapshot": [None]})
        return found

    monkeypatch.setattr(templates, "get", get_while_others_write)
    backend = _ReattachBackend()
    await _registry(sqlite_sp, backend, monkeypatch).get_workspace("w-old")

    assert backend.template.description == "the other process's snapshot"
    stored = await rows.get("w-old")
    assert (stored.name, stored.template_snapshot.description) == ("renamed meanwhile", "the other process's snapshot")


async def test_a_failed_backfill_still_reattaches_from_the_template(sqlite_sp, monkeypatch, caplog):
    await _legacy_row(sqlite_sp, template=WorkspaceTemplate(id="t", provider_id="prov1", description="the recipe"))

    async def write_fails(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(sqlite_sp.get_storage(WorkspaceRow), "patch_if", write_fails)
    backend = _ReattachBackend()
    with caplog.at_level(logging.WARNING, logger="primer.api.registries.workspace_registry"):
        assert await _registry(sqlite_sp, backend, monkeypatch).get_workspace("w-old") == "reattached-handle"

    assert backend.template.description == "the recipe"
    assert "writing the missing template snapshot failed" in caplog.text


async def test_a_row_without_a_snapshot_whose_template_is_gone_still_cannot_reattach(sqlite_sp, monkeypatch, caplog):
    await _legacy_row(sqlite_sp, template=None)

    class _NoTemplateNoHandle(_ReattachBackend):
        async def get(self, workspace_id, *, template=None):
            self.template = template
            return None

    backend = _NoTemplateNoHandle()
    with caplog.at_level(logging.WARNING, logger="primer.api.registries.workspace_registry"), pytest.raises(NotFoundError):
        await _registry(sqlite_sp, backend, monkeypatch).get_workspace("w-old")

    assert backend.template is None
    assert "no longer exists" in caplog.text
    assert (await sqlite_sp.get_storage(WorkspaceRow).get("w-old")).template_snapshot is None
