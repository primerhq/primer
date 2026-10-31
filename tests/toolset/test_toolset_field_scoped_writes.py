"""Harness and document tools write only the fields they change, fenced on what they read (AG-L5).

The harness tools rewrote every field of the harness they read, so an operation enqueued in between got ``pending_operation`` reset
(its lease left on a row the worker never runs). ``update_document`` wrote the whole document, undoing a ``move_document`` in
between. Each now writes only the fields it changes, fenced on what it read; a refused fence answers ``type=conflict`` and writes
nothing.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from primer.harness.hashes import hash_overrides, operation_input_digests
from primer.model.access import Access
from primer.model.collection import Document
from primer.model.common import dump_for_storage
from primer.model.harness import Harness, HarnessDirection, HarnessOperation, HarnessStatus
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.harness import build_harness_toolset_provider
from tests.toolset.test_switch_binding_keeps_concurrent_writes import _race_after_read
from tests.toolset.test_system_document_tools import _call, _put, world  # noqa: F401 -- the document fixture


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "s.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


# --- harness tools (AG-L5) ----------------------------------------------------------------------------------------------


class _Bus:
    def __init__(self) -> None:
        self.published: list[str] = []

    async def publish(self, key: str, payload: Any = None) -> None:
        self.published.append(key)


HID = "hns_aabbcc001"

# tool -> (harness fields, arguments, the fields the call changes and their values given the stored row)
CASES: dict[str, tuple[dict, dict, Any]] = {
    "update": ({}, {"name": "New", "ref": "dev"}, lambda h: {"name": "New", "ref": "dev", "overrides_dirty": True}),
    "update_overrides": (
        {"overrides_schema": {"type": "object"}}, {"overrides": {"a": 1}},
        lambda h: {"overrides": {"a": 1}, "overrides_hash": hash_overrides({"a": 1}), "overrides_dirty": False},
    ),
    "fetch": ({}, {}, lambda h: {"pending_operation": HarnessOperation.FETCH}),
    "uninstall": ({"status": HarnessStatus.INSTALLED}, {},
                  lambda h: {"pending_operation": HarnessOperation.UNINSTALL, "uninstall_cascade": True}),
    "install": (
        {"status": HarnessStatus.READY, "overrides_schema": {"type": "object"}}, {"overrides": {}},
        lambda h: {"pending_operation": HarnessOperation.INSTALL, "operation_inputs": operation_input_digests(h)},
    ),
    "sync": (
        {"status": HarnessStatus.INSTALLED, "available_bundle_hash": "abc"}, {"overrides": {}},
        lambda h: {"pending_operation": HarnessOperation.SYNC, "operation_inputs": operation_input_digests(h)},
    ),
}


async def _harness_world(sp, fields: dict):
    storage = sp.get_storage(Harness)
    harness = Harness(**{
        "id": HID, "slug": "test-harness", "name": "H", "git_url": "https://github.com/example/repo", "status": HarnessStatus.DRAFT,
        "direction": HarnessDirection.INBOUND, "created_at": datetime.now(UTC), "access": Access(public=True), **fields,
    })
    await storage.create(harness)
    bus = _Bus()
    return storage, build_harness_toolset_provider(storage_provider=sp, event_bus=bus), bus


@pytest.mark.parametrize("tool", list(CASES))
async def test_harness_tool_writes_what_it_changes(sp, tool) -> None:
    fields, args, changed = CASES[tool]
    storage, toolset, bus = await _harness_world(sp, fields)
    stored = await storage.get(HID)
    before = dump_for_storage(stored)
    expected = dump_for_storage(stored.model_copy(update=changed(stored)))
    res = await toolset.call(tool_name=tool, arguments={"id": HID, **args})
    assert not res.is_error, res.output
    after = await storage.get(HID)
    assert json.loads(res.output) == after.model_dump(mode="json")
    got = dump_for_storage(after)
    assert {k: got[k] for k in got if got[k] != before.get(k)} == {k: expected[k] for k in expected if expected[k] != before.get(k)}
    assert bus.published == (["harness-claimable"] if tool in ("fetch", "uninstall", "install", "sync") else [])


@pytest.mark.parametrize("tool", list(CASES))
async def test_harness_tool_keeps_an_operation_enqueued_in_between(sp, monkeypatch, tool) -> None:
    fields, args, _ = CASES[tool]
    storage, toolset, bus = await _harness_world(sp, fields)

    async def enqueue():  # the REST twin enqueues a sync between the tool's read and its write
        assert await storage.patch_if(HID, {"pending_operation": "sync"}, where={"pending_operation": [None]}) is not None

    _race_after_read(monkeypatch, storage, enqueue)
    res = await toolset.call(tool_name=tool, arguments={"id": HID, **args})
    monkeypatch.undo()
    after = await storage.get(HID)
    assert after.pending_operation is HarnessOperation.SYNC
    assert res.is_error and json.loads(res.output)["type"] == "conflict"
    assert bus.published == []


# --- update_document (AG-L5) --------------------------------------------------------------------------------------------


async def test_update_document_writes_title_and_meta_only(world) -> None:  # noqa: F811
    provider, toolset, _ = world
    doc_id = await _put(toolset, "page", "the body")
    _, row = await _call(toolset, "get_document", id=doc_id)
    before = dump_for_storage(await provider.get_storage(Document).get(doc_id))
    is_error, updated = await _call(toolset, "update_document", id=doc_id, entity={**row, "title": "T", "meta": {"k": "v"}})
    assert not is_error, updated
    after = await provider.get_storage(Document).get(doc_id)
    assert updated == after.model_dump(mode="json")
    got = dump_for_storage(after)
    assert {k for k in got if got[k] != before.get(k)} == {"title", "meta", "updated_at"}
    assert (got["title"], got["meta"]) == ("T", {"k": "v"})


async def test_update_document_does_not_undo_a_move_in_between(world, monkeypatch) -> None:  # noqa: F811
    provider, toolset, _ = world
    doc_id = await _put(toolset, "page", "the body")
    _, row = await _call(toolset, "get_document", id=doc_id)

    async def move():
        is_error, body = await _call(toolset, "move_document", collection_id="kb-1", **{"from": "page", "to": "moved"})
        assert not is_error, body

    _race_after_read(monkeypatch, provider.get_storage(Document), move)
    is_error, body = await _call(toolset, "update_document", id=doc_id, entity={**row, "title": "T"})
    monkeypatch.undo()
    after = await provider.get_storage(Document).get(doc_id)
    assert after.path == "moved"
    assert is_error and body["type"] == "conflict"
    assert after.title != "T"
