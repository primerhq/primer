"""Tests for the ordered data-migration runner.

The runner operates through ``Storage[T]`` handles rather than raw SQL, so
these tests exercise it against the SQLite backend and the behaviour holds
for Postgres by construction.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.model.provider import SqliteConfig
from primer.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    run_migrations,
)
from primer.storage.sqlite import SqliteStorageProvider

# asyncio_mode = "auto" in pyproject.toml, so async tests need no marker.


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[StorageProvider]:
    provider = SqliteStorageProvider(
        SqliteConfig(path=str(tmp_path / "migrations.sqlite"))
    )
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


# ---------------------------------------------------------------------------
# Registry invariants
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_versions_are_contiguous_from_one(self) -> None:
        versions = [m.version for m in MIGRATIONS]
        assert versions == list(range(1, len(MIGRATIONS) + 1))

    def test_versions_are_unique(self) -> None:
        versions = [m.version for m in MIGRATIONS]
        assert len(set(versions)) == len(versions)

    def test_latest_version_matches_registry(self) -> None:
        assert LATEST_VERSION == len(MIGRATIONS)

    def test_every_migration_has_a_description(self) -> None:
        assert all(m.description.strip() for m in MIGRATIONS)


# ---------------------------------------------------------------------------
# Runner behaviour
# ---------------------------------------------------------------------------


class TestRunMigrations:
    async def test_fresh_install_baselines_without_applying(
        self, sp: StorageProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh install has nothing to transform, so it baselines."""
        applied: list[int] = []
        for migration in MIGRATIONS:
            monkeypatch.setattr(
                migration,
                "apply",
                _recording_apply(migration.version, applied),
                raising=False,
            )

        result = await run_migrations(sp, is_fresh_install=True)

        assert result == LATEST_VERSION
        assert applied == []
        state = await sp.get_system_state()
        assert state.schema_version == LATEST_VERSION
        assert state.last_migration_at is not None

    async def test_existing_install_converges_on_latest(
        self, sp: StorageProvider
    ) -> None:
        result = await run_migrations(sp, is_fresh_install=False)

        assert result == LATEST_VERSION
        state = await sp.get_system_state()
        assert state.schema_version == LATEST_VERSION

    async def test_pending_migration_runs_and_stamps(
        self, sp: StorageProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A migration above the stored version runs and advances it.

        Driven with a synthetic registry rather than the real one so the
        test keeps meaning as migrations are added: with schema_version
        defaulting to 1, whether any SHIPPED migration is pending depends
        on how many exist.
        """
        applied: list[int] = []
        fake = _FakeMigration(version=2, sink=applied)
        monkeypatch.setattr(
            "primer.storage.migrations.MIGRATIONS", (fake,),
        )
        monkeypatch.setattr("primer.storage.migrations.LATEST_VERSION", 2)

        result = await run_migrations(sp, is_fresh_install=False)

        assert applied == [2]
        assert result == 2
        state = await sp.get_system_state()
        assert state.schema_version == 2
        assert state.last_migration_at is not None

    async def test_already_applied_migrations_are_skipped(
        self, sp: StorageProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """schema_version = N means migrations 1..N have already run."""
        await sp.set_schema_version(LATEST_VERSION)
        applied: list[int] = []
        for migration in MIGRATIONS:
            monkeypatch.setattr(
                migration,
                "apply",
                _recording_apply(migration.version, applied),
                raising=False,
            )

        await run_migrations(sp, is_fresh_install=False)

        assert applied == []

    async def test_double_apply_is_idempotent(self, sp: StorageProvider) -> None:
        await run_migrations(sp, is_fresh_install=False)
        first = await sp.get_system_state()
        await run_migrations(sp, is_fresh_install=False)
        second = await sp.get_system_state()

        assert second.schema_version == first.schema_version == LATEST_VERSION

    async def test_future_version_warns_and_does_not_downgrade(
        self, sp: StorageProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Older code against a newer DB must refuse rather than guess."""
        future = LATEST_VERSION + 5
        await sp.set_schema_version(future)

        with caplog.at_level(logging.WARNING):
            result = await run_migrations(sp, is_fresh_install=False)

        assert result == future
        state = await sp.get_system_state()
        assert state.schema_version == future
        assert any(
            "newer than this build" in record.getMessage()
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# set_schema_version storage contract
# ---------------------------------------------------------------------------


class TestSetSchemaVersion:
    async def test_stamps_version_and_timestamp(self, sp: StorageProvider) -> None:
        await sp.set_schema_version(7)
        state = await sp.get_system_state()
        assert state.schema_version == 7
        assert state.last_migration_at is not None

    async def test_overwrites_previous_value(self, sp: StorageProvider) -> None:
        await sp.set_schema_version(3)
        await sp.set_schema_version(4)
        state = await sp.get_system_state()
        assert state.schema_version == 4


def _recording_apply(version: int, sink: list[int]):
    async def _apply(_sp: StorageProvider) -> None:
        sink.append(version)

    return _apply


class _FakeMigration:
    """Stand-in migration so runner tests do not depend on the real chain."""

    def __init__(self, *, version: int, sink: list[int]) -> None:
        self.version = version
        self.description = f"fake migration {version}"
        self._sink = sink

    async def apply(self, _sp: StorageProvider) -> None:
        self._sink.append(self.version)


# ---------------------------------------------------------------------------
# m003: the session cutover left channel and collection rows unreadable
# ---------------------------------------------------------------------------


async def test_m003_legacy_channel_loads_through_the_live_model(sp):
    """A channel written before bindings must survive the upgrade.

    The three agent-routing fields moved out of ``config.chats`` and
    ``ChatConfig`` forbids extras, so without the migration the live
    model rejects the row outright.
    """
    from primer.model.channel import Channel as LiveChannel
    from primer.storage.migrations.m003_session_cutover import Channel as ChannelView

    legacy = sp.get_storage(ChannelView)
    await legacy.create(
        ChannelView(
            id="channel-legacy",
            provider_id="slack-1",
            provider="slack",
            external_id="C0123",
            config={
                "chats": {
                    "enabled": True,
                    "relay_mode": "all",
                    "default_agent": None,
                    "allowed_agents": [],
                    "allow_agent_switch": False,
                }
            },
        )
    )

    with pytest.raises(Exception):
        await sp.get_storage(LiveChannel).get("channel-legacy")

    await run_migrations(sp, is_fresh_install=False)

    row = await sp.get_storage(LiveChannel).get("channel-legacy")
    assert row is not None
    # The settings that outlived the cutover are still there.
    assert row.config.chats.enabled is True
    assert row.config.chats.relay_mode == "all"


async def test_m003_legacy_collection_search_moves_under_search(sp):
    """embedder and search_provider_id move into the nested search block."""
    from primer.model.collection import Collection as LiveCollection
    from primer.storage.migrations.m003_session_cutover import (
        Collection as CollectionView,
    )

    legacy = sp.get_storage(CollectionView)
    await legacy.create(
        CollectionView(
            id="collection-legacy",
            description="a wiki written before the move",
            embedder={"provider_id": "emb-1", "model": "text-embedding-3-small"},
            search_provider_id="vec-1",
            search={
                "cer": {"provider_id": "cer-1", "model": "cross-encoder/ms-marco"},
                "mmr": {"lambda_mult": 0.5},
            },
        )
    )

    with pytest.raises(Exception):
        await sp.get_storage(LiveCollection).get("collection-legacy")

    await run_migrations(sp, is_fresh_install=False)

    row = await sp.get_storage(LiveCollection).get("collection-legacy")
    assert row is not None
    assert row.search is not None
    assert row.search.embedder.provider_id == "emb-1"
    assert row.search.embedder.model == "text-embedding-3-small"
    assert row.search.vector_store_provider_id == "vec-1"
    # The rerank config rides along under its new name; mmr has no home.
    assert row.search.cross_encoder is not None
    assert row.search.cross_encoder.provider_id == "cer-1"
    # The vectors the old build wrote are still in the store, so the row
    # must not claim an index pass is in flight.
    assert row.search.state == "ready"


async def test_m003_collection_without_a_vector_store_stays_grep_only(sp):
    """A row with no complete legacy config is not given an invented one."""
    from primer.model.collection import Collection as LiveCollection
    from primer.storage.migrations.m003_session_cutover import (
        Collection as CollectionView,
    )

    await sp.get_storage(CollectionView).create(
        CollectionView(
            id="collection-grep",
            description="never had a vector store",
            search={"mmr": {"lambda_mult": 0.5}},
        )
    )

    await run_migrations(sp, is_fresh_install=False)

    row = await sp.get_storage(LiveCollection).get("collection-grep")
    assert row is not None
    assert row.search is None


async def test_m003_is_idempotent(sp):
    """Re-running over already-migrated rows changes nothing."""
    from primer.model.collection import Collection as LiveCollection
    from primer.storage.migrations.m003_session_cutover import (
        Collection as CollectionView,
        M003SessionCutover,
    )

    await sp.get_storage(CollectionView).create(
        CollectionView(
            id="collection-twice",
            description="migrated twice",
            embedder={"provider_id": "emb-1", "model": "text-embedding-3-small"},
            search_provider_id="vec-1",
        )
    )

    await run_migrations(sp, is_fresh_install=False)
    first = await sp.get_storage(LiveCollection).get("collection-twice")

    await M003SessionCutover().apply(sp)
    second = await sp.get_storage(LiveCollection).get("collection-twice")

    assert first is not None and second is not None
    assert first.search.model_dump() == second.search.model_dump()


async def test_m003_legacy_document_gets_a_slug_and_parent(sp):
    """Documents identified by name gain slug and parent_id from path.

    Without this the tree endpoint 500s on every collection an upgraded
    install already had, because slug is required on the live model.
    """
    from primer.model.collection import Document as LiveDocument
    from primer.storage.migrations.m004_document_slugs import (
        Document as DocumentView,
    )

    legacy = sp.get_storage(DocumentView)
    await legacy.create(
        DocumentView(
            id="document-root",
            collection_id="coll-1",
            name="guides",
            path="guides",
        )
    )
    await legacy.create(
        DocumentView(
            id="document-child",
            collection_id="coll-1",
            name="onboarding.md",
            path="guides/onboarding.md",
        )
    )

    with pytest.raises(Exception):
        await sp.get_storage(LiveDocument).get("document-child")

    await run_migrations(sp, is_fresh_install=False)

    docs = sp.get_storage(LiveDocument)
    root = await docs.get("document-root")
    child = await docs.get("document-child")
    assert root is not None and child is not None
    assert root.slug == "guides"
    assert root.parent_id is None
    assert child.slug == "onboarding.md"
    # The child is wired to the row that owns its parent path, not to the
    # path string, so the tree can walk it.
    assert child.parent_id == "document-root"
    # path is untouched: it was already correct and the tree reads it.
    assert child.path == "guides/onboarding.md"


async def test_m005_synthesises_the_directory_a_flat_document_names(sp):
    """Pre-tree data is flat: a nested path with no row at its parent.

    Demoting the child to a root would keep it readable but leave its path
    inconsistent with its slug, so the first move would compute a new path
    and strand the body under the old one. The directory is created instead.
    """
    from primer.knowledge.tree import DocumentTreeService
    from primer.model.collection import Document as LiveDocument
    from primer.storage.migrations.m005_document_directories import (
        Document as DocumentView,
    )

    await sp.get_content_store().ensure_schema()
    await sp.get_storage(DocumentView).create(
        DocumentView(
            id="document-flat",
            collection_id="coll-1",
            name="deploy-bot.md",
            path="cookbook/deploy-bot.md",
        )
    )
    await sp.get_content_store().upsert(
        document_id="document-flat", collection_id="coll-1",
        path="cookbook/deploy-bot.md", content="how to deploy",
    )

    await run_migrations(sp, is_fresh_install=False)

    docs = sp.get_storage(LiveDocument)
    child = await docs.get("document-flat")
    assert child is not None
    assert child.slug == "deploy-bot.md"
    assert child.parent_id is not None, "the directory should have been created"

    parent = await docs.get(child.parent_id)
    assert parent is not None
    assert parent.slug == "cookbook"
    assert parent.path == "cookbook"
    assert parent.parent_id is None

    # The synthesised directory resolves as a parent, which is what the
    # tree service needs to list or create anything beneath it.
    tree = DocumentTreeService(sp)
    page = await tree.read(collection_id="coll-1", path="cookbook/deploy-bot.md")
    assert page.body == "how to deploy"
    # path is still parent.path + "/" + slug, so a move cannot strand the body.
    assert child.path == f"{parent.path}/{child.slug}"

    # Re-running must not build a second directory alongside the first.
    from primer.model.storage import OffsetPage
    from primer.storage.migrations.m005_document_directories import (
        M005DocumentDirectories,
    )

    before = (await docs.list(OffsetPage(offset=0, length=100))).items
    await M005DocumentDirectories().apply(sp)
    after = (await docs.list(OffsetPage(offset=0, length=100))).items
    assert len(after) == len(before)
    assert {d.id for d in after} == {d.id for d in before}


async def test_m005_adopts_a_directory_another_pod_created_first(sp):
    """Every pod runs the chain on boot, so two can build the same directory.

    The content store's path uniqueness makes the loser lose. It must adopt
    the winner's node and clean up the row it had already written, rather
    than leaving a document behind that nothing can resolve.
    """
    from primer.model.collection import Document as LiveDocument
    from primer.model.except_ import ConflictError
    from primer.model.storage import OffsetPage
    from primer.storage.migrations.m005_document_directories import (
        Document as DocumentView,
        M005DocumentDirectories,
    )

    content = sp.get_content_store()
    await content.ensure_schema()
    docs = sp.get_storage(DocumentView)
    await docs.create(
        DocumentView(
            id="document-child",
            collection_id="coll-1",
            path="cookbook/x.md",
            slug="x.md",
        )
    )
    await content.upsert(
        document_id="document-child", collection_id="coll-1",
        path="cookbook/x.md", content="body",
    )

    # The other pod wins the directory: the path is taken by its node.
    winner_id = LiveDocument(collection_id="_", slug="x", path="x").id
    real_upsert = type(content).upsert
    seeded: dict[str, bool] = {}

    async def upsert_that_loses(self, *, document_id, collection_id, path, content, conn=None):
        if path == "cookbook" and not seeded.get("done"):
            seeded["done"] = True
            await real_upsert(
                self, document_id=winner_id, collection_id=collection_id,
                path=path, content="", conn=conn,
            )
            raise ConflictError(f"path {path!r} is taken")
        return await real_upsert(
            self, document_id=document_id, collection_id=collection_id,
            path=path, content=content, conn=conn,
        )

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(type(content), "upsert", upsert_that_loses)
    try:
        await M005DocumentDirectories().apply(sp)
    finally:
        mp.undo()

    assert seeded.get("done"), "the losing upsert never fired"

    live = sp.get_storage(LiveDocument)
    child = await live.get("document-child")
    assert child is not None
    # It adopted the winner rather than pointing at the row it wrote.
    assert child.parent_id == winner_id

    # And it left nothing behind. Every surviving row's path must resolve
    # to that row: an orphan's path resolves to the winner instead, which a
    # mere not-None check would happily accept.
    rows = (await docs.list(OffsetPage(offset=0, length=100))).items
    for row in rows:
        if row.path:
            owner = await content.resolve_id(row.collection_id, row.path)
            assert owner == row.id, (
                f"document {row.id} sits at {row.path}, which resolves to {owner}"
            )
