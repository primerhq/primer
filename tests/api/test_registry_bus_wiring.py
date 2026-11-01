"""GX-3: the semantic-search, web-search, web-fetch, speech and workspace-provider registries evict on the invalidation bus, so an edit in one process
reaches every other one. Two registries on one in-memory bus stand for the API process and a worker."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.api.registries.semantic_search_registry import SemanticSearchRegistry
from primer.api.registries.speech_registry import SpeechRegistry
from primer.api.registries.web_fetch_registry import WebFetchRegistry
from primer.api.registries.web_search_registry import WebSearchRegistry
from primer.api.registries.workspace_registry import WorkspaceRegistry
from primer.coordinator.in_memory import InMemoryInvalidationBus
from primer.int.coordinator import InvalidationTopic
from primer.model.except_ import NotFoundError
from primer.model.workspace import LocalWorkspaceConfig, WorkspaceProvider, WorkspaceProviderType
from tests.conftest import _FakeStorageProvider


class _Rows:
    """The one database both processes read."""

    def __init__(self) -> None:
        self.rows = {"p1": SimpleNamespace(id="p1", version="v1")}

    async def get(self, _id: str):
        return self.rows.get(_id)


class _Instance:
    def __init__(self, row) -> None:
        self.version = row.version

    async def initialize(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


SIMPLE = [
    (SemanticSearchRegistry, "get_provider", {}, InvalidationTopic.SEMANTIC_SEARCH_PROVIDER),
    (WebSearchRegistry, "get", {}, InvalidationTopic.WEB_SEARCH_PROVIDER),
    (WebFetchRegistry, "get", {}, InvalidationTopic.WEB_FETCH_PROVIDER),
    (SpeechRegistry, "get", {"label": "tts"}, InvalidationTopic.TEXT_TO_SPEECH_PROVIDER),
    (SpeechRegistry, "get", {"label": "stt"}, InvalidationTopic.SPEECH_TO_TEXT_PROVIDER),
]


@pytest.mark.parametrize(("cls", "getter", "kwargs", "topic"), SIMPLE, ids=["semantic", "web_search", "web_fetch", "tts", "stt"])
async def test_an_edit_in_one_process_reaches_the_other(cls, getter, kwargs, topic):
    db, bus = _Rows(), InMemoryInvalidationBus()
    api = cls(storage=db, factory=_Instance, retire_idle_seconds=0.01, **kwargs)
    worker = cls(storage=db, factory=_Instance, retire_idle_seconds=0.01, **kwargs)
    await api.bind_invalidation_bus(bus)
    await worker.bind_invalidation_bus(bus)
    assert (await getattr(worker, getter)("p1")).version == "v1"

    db.rows["p1"] = SimpleNamespace(id="p1", version="v2")        # a rotated key, a new host
    await api.invalidate("p1")                                    # the router's on_update hook, in the API process only
    assert (await getattr(worker, getter)("p1")).version == "v2"

    del db.rows["p1"]                                             # DELETE
    await api.invalidate("p1")
    with pytest.raises(NotFoundError):
        await getattr(worker, getter)("p1")
    await api.aclose()
    await worker.aclose()


@pytest.mark.parametrize(("cls", "getter", "kwargs", "topic"), SIMPLE, ids=["semantic", "web_search", "web_fetch", "tts", "stt"])
async def test_an_unbound_registry_still_evicts_locally_and_binding_is_idempotent(cls, getter, kwargs, topic):
    db = _Rows()
    reg = cls(storage=db, factory=_Instance, retire_idle_seconds=0.01, **kwargs)
    first = await getattr(reg, getter)("p1")
    await reg.invalidate("p1")
    assert await getattr(reg, getter)("p1") is not first
    bus = InMemoryInvalidationBus()
    await reg.bind_invalidation_bus(bus)
    await reg.bind_invalidation_bus(bus)                          # idempotent: one subscription
    assert len(bus._handlers[topic]) == 1
    await reg.aclose()
    assert len(bus._handlers[topic]) == 0                         # unsubscribed at shutdown
    await reg.invalidate("p1")                                    # and an unbound registry evicts here again, never publishes to no one


async def test_workspace_provider_edits_reach_every_process():
    sp = _FakeStorageProvider()
    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(id="local", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-gx3")),
    )
    built: list[object] = []

    class _Backend:
        async def initialize(self) -> None:
            built.append(self)

        async def aclose(self) -> None:
            return None

    bus = InMemoryInvalidationBus()
    api, worker = WorkspaceRegistry(sp, factory=lambda p: _Backend()), WorkspaceRegistry(sp, factory=lambda p: _Backend())  # type: ignore[arg-type]
    await api.bind_invalidation_bus(bus)
    await worker.bind_invalidation_bus(bus)
    first = await worker.get_backend("local")
    assert await worker.get_backend("local") is first
    await api.invalidate("local")
    assert await worker.get_backend("local") is not first
    await api.aclose()
    await worker.aclose()


def test_every_registry_topic_is_published_and_subscribed_somewhere():
    import pathlib

    import primer

    root = pathlib.Path(primer.__file__).parent
    text = "\n".join(p.read_text() for p in (root / "api" / "registries").glob("*.py"))
    for topic in ("SEMANTIC_SEARCH_PROVIDER", "WORKSPACE_PROVIDER", "WEB_SEARCH_PROVIDER", "WEB_FETCH_PROVIDER",
                  "TEXT_TO_SPEECH_PROVIDER", "SPEECH_TO_TEXT_PROVIDER"):
        assert f"InvalidationTopic.{topic}" in text, topic
