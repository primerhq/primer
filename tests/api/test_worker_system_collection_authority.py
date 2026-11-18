"""A reduced worker HTTP surface cannot replace the shared API wiki."""

from __future__ import annotations

import copy

import pytest

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.api.registries import provider_registry
from primer.knowledge.wiki.stamp import read_stamp
from primer.model.collection import Document
from primer.model.scheduler import RuntimeMode
from primer.model.tool_approval import ToolApprovalPolicy
from tests.conftest import _FakeStorageProvider


def _app(monkeypatch, storage, mode):
    # Each real process starts with only its own generated-provider IDs.
    # Without resetting this union, an API constructed earlier in this test
    # interpreter would make the reduced worker appear degraded by accident.
    monkeypatch.setattr(provider_registry, "_GENERATED_API_TOOLSET_IDS", frozenset())
    monkeypatch.setattr("primer.api.app._build_storage_provider", lambda _: storage)
    return create_app(AppConfig(runtime_mode=mode, auto_bootstrap=False))


def _documents(storage):
    return copy.deepcopy((
        storage.get_storage(Document)._data,
        storage.get_content_store()._rows,
    ))


@pytest.mark.parametrize("api_mode", [RuntimeMode.API, RuntimeMode.API_PLUS_WORKER])
async def test_worker_start_preserves_complete_api_documents_and_stamp(monkeypatch, api_mode):
    storage = _FakeStorageProvider()
    api = _app(monkeypatch, storage, api_mode)
    async with api.router.lifespan_context(api):
        before = _documents(storage)
        stamp = await read_stamp(storage)
        assert stamp is not None
        paths = {row.path for row in before[0].values()}
        assert "reference/tools/agents/agent_status" in paths
        assert "reference/tools/documents/list" in paths

    for _ in range(2):
        worker = _app(monkeypatch, storage, RuntimeMode.WORKER)
        assert "/v1/sessions" not in worker.openapi()["paths"]
        async with worker.router.lifespan_context(worker):
            assert worker.state.worker_pool is not None
            assert _documents(storage) == before
            assert await read_stamp(storage) == stamp
        assert _documents(storage) == before
        assert await read_stamp(storage) == stamp


async def test_worker_first_boot_keeps_other_ensures_but_does_not_publish_a_partial_wiki(monkeypatch):
    storage = _FakeStorageProvider()
    worker = _app(monkeypatch, storage, RuntimeMode.WORKER)
    async with worker.router.lifespan_context(worker):
        assert worker.state.worker_pool is not None
        assert storage.get_storage(ToolApprovalPolicy)._data
        assert not storage.get_storage(Document)._data
        assert not storage.get_content_store()._rows
        assert await read_stamp(storage) is None

    api = _app(monkeypatch, storage, RuntimeMode.API)
    async with api.router.lifespan_context(api):
        assert await read_stamp(storage) is not None
        assert any(
            row.path == "reference/tools/documents/list"
            for row in storage.get_storage(Document)._data.values()
        )


async def test_serve_only_start_preserves_the_api_wiki_too(monkeypatch):
    storage = _FakeStorageProvider()
    api = _app(monkeypatch, storage, RuntimeMode.API)
    async with api.router.lifespan_context(api):
        before = _documents(storage)
        stamp = await read_stamp(storage)
        assert stamp is not None

    monkeypatch.setenv("PRIMER_SERVE_ONLY", "1")
    serving = _app(monkeypatch, storage, RuntimeMode.API)
    assert "/v1/sessions" not in serving.openapi()["paths"]
    async with serving.router.lifespan_context(serving):
        assert serving.state.worker_pool is None
        assert _documents(storage) == before
        assert await read_stamp(storage) == stamp
