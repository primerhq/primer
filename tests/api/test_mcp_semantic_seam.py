"""The production find_tools semantic seam: system wiki, lexical fallback, caller Person (seam spec §2).

``_build_semantic_search`` is the only production wiring of the optional
``ExposureDeps.semantic_search`` seam: it reads the system collection, runs
the collection query ladder over ``reference/tools`` in auto mode under the
request's actor Person, and degrades every failure to ``[]`` so the MCP
mount never depends on the index.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.api._app_mcp import _build_semantic_search
from primer.knowledge.collection_query import QueryOutcome
from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID
from primer.mcp.server import current_actor
from primer.model.access import Access
from primer.model.collection import Collection
from primer.model.principal import Principal

HIT = {"path": "reference/tools/misc/alpha", "excerpt": "x", "score": 1.0}


def _user_actor() -> Principal:
    return Principal(type="user", id="u-1", user_id="u-1", display="U", role="user", source="local")


class _SeamApp:
    """A minimal app.state stand-in recording what the seam passes down."""

    def __init__(self, *, registry, collection, raising_query=False, raising_storage=False):
        self.calls: dict = {}
        self._collection = collection
        self._raising = raising_query
        self._raising_storage = raising_storage
        self.state = SimpleNamespace(
            semantic_search_registry=registry,
            storage_provider=SimpleNamespace(get_storage=self._storage),
            provider_registry=SimpleNamespace(),
        )

    def _storage(self, _model):
        return SimpleNamespace(get=self._get_collection)

    async def _get_collection(self, _id):
        assert _id == SYSTEM_COLLECTION_ID
        if self._raising_storage:
            raise RuntimeError("storage down")
        return self._collection

    async def _fake_query(self, **kwargs):
        self.calls.update(kwargs)
        if self._raising:
            raise RuntimeError("index unavailable")
        return QueryOutcome([dict(HIT)], "semantic")
    def install(self, monkeypatch):
        """Patch the ladder the way production reaches it (in-function import)."""
        import primer.knowledge.collection_query as collection_query

        monkeypatch.setattr(collection_query, "query_collection", self._fake_query)


def _system_collection() -> Collection:
    return Collection(
        id=SYSTEM_COLLECTION_ID,
        description="The Primer wiki",
        system=True,
        access=Access(public=True),
    )


@pytest.mark.asyncio
async def test_seam_absent_without_registry(monkeypatch):
    app = _SeamApp(registry=None, collection=_system_collection())
    assert _build_semantic_search(app) is None


@pytest.mark.asyncio
async def test_seam_returns_reference_paths_and_passes_person(monkeypatch):
    app = _SeamApp(registry=SimpleNamespace(), collection=_system_collection())
    app.install(monkeypatch)

    search = _build_semantic_search(app)
    assert search is not None
    actor_token = current_actor.set(_user_actor())
    try:
        paths = await search("alpha", 10)
    finally:
        current_actor.reset(actor_token)

    assert paths == [HIT["path"]]
    calls = app.calls
    assert calls["path_prefix"] == "reference/tools"
    assert calls["mode"] == "auto"
    assert calls["query"] == "alpha"
    assert calls["limit"] == 10
    assert calls["person"].role == "user"


@pytest.mark.asyncio
async def test_seam_swallows_failures_to_empty(monkeypatch):
    # No system collection row: the seam answers [] without querying.
    app = _SeamApp(registry=SimpleNamespace(), collection=None)
    assert await _build_semantic_search(app)("alpha", 10) == []

    # A raising ladder rung: the seam degrades to [] instead of raising.
    app = _SeamApp(registry=SimpleNamespace(), collection=_system_collection(), raising_query=True)
    app.install(monkeypatch)
    assert await _build_semantic_search(app)("alpha", 10) == []


@pytest.mark.asyncio
async def test_seam_storage_failure_degrades_to_empty(monkeypatch):
    """A raising storage read degrades to []: the collection lookup is
    inside the guard, so a storage failure never escapes the seam."""
    app = _SeamApp(
        registry=SimpleNamespace(), collection=_system_collection(),
        raising_storage=True,
    )
    assert await _build_semantic_search(app)("alpha", 10) == []
    assert app.calls == {}


@pytest.mark.asyncio
async def test_seam_person_restricted_without_actor(monkeypatch):
    app = _SeamApp(registry=SimpleNamespace(), collection=_system_collection())
    app.install(monkeypatch)

    # current_actor is left at its default (None): the Person is restricted.
    await _build_semantic_search(app)("alpha", 10)

    assert app.calls["person"].role == "restricted"
