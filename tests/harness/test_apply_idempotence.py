"""Harness apply idempotence and uninstall cleanup (KN-3, KN-4, KN-5, KN-19d).

KN-3: a sync that failed part-way kept the old snapshot (correctly) but left the rows it did create, so every later
sync re-ran those creates and failed on its own rows (and re-ran deletes that failed on rows already gone).
KN-4: uninstall and sync deletes removed collection and document rows with a plain delete, leaving the collection's
documents, their content rows and the vectors behind.
KN-5: uninstall swallowed every delete error and then dropped the snapshot and the harness row, so nothing was left to
retry from; a ``cascade=False`` uninstall left every kept row stamped with the deleted harness's id (409 for ever).
KN-19d: the ``apply_id_conflict`` rollback of an install deleted the entity rows and not the documents' content rows.

Real SQLite storage (a duplicate create is a real ``ConflictError``), fake vector store and registry.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio

from primer.harness.dispatch import HarnessDispatchDeps, run_one_harness_operation
from primer.harness.service import apply_install, apply_sync, apply_uninstall
from primer.knowledge.tree import DocumentTreeService
from primer.model.agent import Agent
from primer.model.collection import Collection, Document
from primer.model.harness import Harness, HarnessOperation, HarnessRendering, HarnessStatus, RenderedEntry
from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType
from primer.storage.factory import StorageProviderFactory


@pytest_asyncio.fixture
async def sp(tmp_path):
    p = StorageProviderFactory.create(StorageProviderConfig(
        provider=StorageProviderType.SQLITE, config=SqliteConfig(path=tmp_path / "t.sqlite")))
    await p.initialize()
    await p.get_content_store().ensure_schema()
    yield p
    await p.aclose()


def _h(hid: str = "acme-id") -> Harness:
    return Harness(id=hid, slug="acme", name="Acme", git_url="https://github.com/x/y", created_at=datetime.now(timezone.utc))


def _agent(name: str, rendered: str | None = None) -> RenderedEntry:
    return RenderedEntry(
        kind="agent", template_name=name, resolved_id=f"acme__{name}", template_source_hash="h",
        rendered_hash=rendered or f"r-{name}", rendered_payload={"description": rendered or name, "model": {"profile_id": "p--m"}},
    )


_SEARCH = {"embedder": {"provider_id": "ep", "model": "m"}, "vector_store_provider_id": "vs"}


def _coll(name: str = "kb", *, search: bool = False) -> RenderedEntry:
    payload: dict[str, Any] = {"description": name}
    if search:
        payload["search"] = _SEARCH
    return RenderedEntry(kind="collection", template_name=name, resolved_id=f"acme__{name}", template_source_hash="h",
                         rendered_hash=f"r-{name}", rendered_payload=payload)


def _doc(name: str, collection: str) -> RenderedEntry:
    return RenderedEntry(kind="document", template_name=name, resolved_id=f"acme__{name}", template_source_hash="h",
                         rendered_hash=f"r-{name}",
                         rendered_payload={"collection_id": collection, "slug": name, "path": name, "title": name})


def _files(**bodies: str) -> dict:
    from primer.harness.template import RenderedFile

    return {
        name: RenderedFile(template_path=f"{name}.yaml", template_name=name, kind="document", source_bytes=b"",
                           rendered_text="", rendered={}, content=body)
        for name, body in bodies.items()
    }


class _Store:
    """A vector store that records what is removed from it."""

    def __init__(self) -> None:
        self.dropped: list[str] = []
        self.deleted: list[tuple[str, str]] = []

    async def drop_collection(self, cid):
        self.dropped.append(cid)

    async def delete(self, cid, did):
        self.deleted.append((cid, did))


class _Registry:
    def __init__(self, store) -> None:
        self.store = store

    async def get_store(self, provider_id):
        return self.store


async def _install(sp, h, entries, files=None, **kw):
    await sp.get_storage(Harness).create(h)
    assert await apply_install(storage_provider=sp, harness=h, entries=entries, rendered_files_by_name=files or {},
                               bundle_hash="b0", overrides_hash="o", schema_hash=None, **kw) is None


async def _sync(sp, h, entries, bundle="b1", files=None, **kw):
    return await apply_sync(storage_provider=sp, harness=h, new_entries=entries, rendered_files_by_name=files or {},
                            bundle_hash=bundle, overrides_hash="o", schema_hash=None, **kw)


async def _snapshot_ids(sp, h):
    r = await sp.get_storage(HarnessRendering).get(h.id)
    return None if r is None else (r.bundle_hash, sorted(e.resolved_id for e in r.entries))


# ---------------------------------------------------------------------------
# Characterization: clean install, sync, uninstall; genuine failures
# ---------------------------------------------------------------------------


async def test_a_clean_install_sync_and_uninstall(sp):
    h = _h()
    await _install(sp, h, [_coll(), _doc("d1", "acme__kb"), _agent("a")], _files(d1="one"))
    assert await _snapshot_ids(sp, h) == ("b0", ["acme__a", "acme__d1", "acme__kb"])
    assert await _sync(sp, h, [_coll(), _doc("d2", "acme__kb"), _agent("a", "r-a2"), _agent("b")], files=_files(d2="two")) is None
    assert await _snapshot_ids(sp, h) == ("b1", ["acme__a", "acme__b", "acme__d2", "acme__kb"])
    assert (await sp.get_storage(Agent).get("acme__a")).description == "r-a2"
    assert await sp.get_storage(Document).get("acme__d1") is None
    assert await sp.get_content_store().get("acme__d1") is None
    assert await sp.get_content_store().get("acme__d2") == "two"
    await apply_uninstall(storage_provider=sp, harness=h, cascade=True)
    for model, rid in [(Agent, "acme__a"), (Agent, "acme__b"), (Document, "acme__d2"), (Collection, "acme__kb")]:
        assert await sp.get_storage(model).get(rid) is None
    assert await sp.get_content_store().get("acme__d2") is None
    assert await sp.get_storage(HarnessRendering).get(h.id) is None
    assert await sp.get_storage(Harness).get(h.id) is None


async def test_a_sync_that_fails_reports_partial_apply_failure_and_keeps_the_snapshot(sp):
    h = _h()
    await _install(sp, h, [])
    await sp.get_storage(Agent).create(Agent(id="acme__b", description="mine", model={"profile_id": "p--m"}))
    err = await _sync(sp, h, [_agent("a"), _agent("b")])
    assert json.loads(err)["code"] == "partial_apply_failure"
    assert [e["id"] for e in json.loads(err)["errors"]] == ["acme__b"]
    assert await _snapshot_ids(sp, h) == ("b0", [])
    assert (await sp.get_storage(Agent).get("acme__b")).description == "mine"   # the hand-made row is untouched


async def test_a_row_another_harness_owns_still_conflicts_on_sync_and_install(sp):
    h = _h()
    await _install(sp, h, [])
    await sp.get_storage(Agent).create(
        Agent(id="acme__b", description="theirs", model={"profile_id": "p--m"}, harness_id="other-id"))
    err = await _sync(sp, h, [_agent("b")])
    assert [e["id"] for e in json.loads(err)["errors"]] == ["acme__b"]
    assert (await sp.get_storage(Agent).get("acme__b")).harness_id == "other-id"
    h2 = _h("h2-id")
    await sp.get_storage(Harness).create(h2)
    err = await apply_install(storage_provider=sp, harness=h2, entries=[_agent("b")], rendered_files_by_name={},
                              bundle_hash="b0", overrides_hash="o", schema_hash=None)
    assert json.loads(err)["code"] == "apply_id_conflict"


async def test_a_cascade_uninstall_without_a_snapshot_removes_only_the_harness(sp):
    h = _h()
    await sp.get_storage(Harness).create(h)
    await apply_uninstall(storage_provider=sp, harness=h, cascade=True)
    assert await sp.get_storage(Harness).get(h.id) is None


# ---------------------------------------------------------------------------
# KN-3: a failed sync no longer wedges every later sync
# ---------------------------------------------------------------------------


async def test_a_sync_after_a_partial_failure_applies_and_uninstall_removes_what_it_created(sp):
    h = _h()
    await _install(sp, h, [])
    await sp.get_storage(Agent).create(Agent(id="acme__b", description="mine", model={"profile_id": "p--m"}))
    err = await _sync(sp, h, [_agent("a"), _agent("b")])
    assert json.loads(err)["code"] == "partial_apply_failure"
    assert (await sp.get_storage(Agent).get("acme__a")).harness_id == h.id
    await sp.get_storage(Agent).delete("acme__b")          # the operator removes the cause
    assert await _sync(sp, h, [_agent("a"), _agent("b")]) is None
    assert await _snapshot_ids(sp, h) == ("b1", ["acme__a", "acme__b"])
    await apply_uninstall(storage_provider=sp, harness=h, cascade=True)
    assert await sp.get_storage(Agent).get("acme__a") is None
    assert await sp.get_storage(Agent).get("acme__b") is None


async def test_a_retried_create_of_its_own_row_writes_the_new_payload(sp):
    h = _h()
    await _install(sp, h, [])
    await sp.get_storage(Agent).create(
        Agent(id="acme__a", description="stale", model={"profile_id": "p--m"}, harness_id=h.id))
    assert await _sync(sp, h, [_agent("a", "fresh")]) is None
    assert (await sp.get_storage(Agent).get("acme__a")).description == "fresh"


async def test_a_sync_delete_of_a_row_already_gone_is_done(sp):
    h = _h()
    await _install(sp, h, [_agent("a"), _agent("b")])
    await sp.get_storage(Agent).delete("acme__a")           # gone already (an earlier failed sync deleted it)
    assert await _sync(sp, h, [_agent("b")]) is None
    assert await _snapshot_ids(sp, h) == ("b1", ["acme__b"])

# ---------------------------------------------------------------------------
# KN-5: a real delete error stops the uninstall; cascade=False releases the kept rows
# ---------------------------------------------------------------------------


class _Bus:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def publish(self, key, payload):
        self.events.append(key)


async def _enqueue_uninstall(sp, h, cascade=True):
    stored = await sp.get_storage(Harness).get(h.id)
    await sp.get_storage(Harness).update(stored.model_copy(update={
        "pending_operation": HarnessOperation.UNINSTALL, "uninstall_cascade": cascade, "status": HarnessStatus.INSTALLED,
    }))


async def test_the_uninstall_operation_removes_the_harness_and_publishes_done(sp):
    h = _h()
    await _install(sp, h, [_agent("a")])
    await _enqueue_uninstall(sp, h)
    bus = _Bus()
    await run_one_harness_operation(HarnessDispatchDeps(storage_provider=sp, event_bus=bus), harness_id=h.id, worker_id="w")
    assert await sp.get_storage(Harness).get(h.id) is None
    assert bus.events == [f"harness:{h.id}:done"]


# ---------------------------------------------------------------------------
# KN-19d: the id-conflict rollback of an install leaves no content row
# ---------------------------------------------------------------------------


async def test_an_id_conflict_rollback_deletes_the_documents_content(sp):
    await sp.get_storage(Agent).create(
        Agent(id="acme__a", description="theirs", model={"profile_id": "p--m"}, harness_id="other-id"))
    h = _h()
    await sp.get_storage(Harness).create(h)
    err = await apply_install(storage_provider=sp, harness=h, entries=[_coll(), _doc("d1", "acme__kb"), _agent("a")],
                              rendered_files_by_name=_files(d1="body"), bundle_hash="b0", overrides_hash="o", schema_hash=None)
    assert json.loads(err)["code"] == "apply_id_conflict"
    assert await sp.get_storage(Document).get("acme__d1") is None
    assert await sp.get_content_store().get("acme__d1") is None
