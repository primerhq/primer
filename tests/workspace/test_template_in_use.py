"""``workspaces_made_from``: which workspaces keep a template from being deleted (lead ruling 6 of plan 08, platform revamp 2026-10).

The REST template route and the ``delete_workspace_template`` tool both ask this one function, so the tests here pin the question and the
sentence once, on a real SQLite store: a workspace whose ``template_id`` names the template blocks it, whichever provider the workspace is on; the
answer counts all of them and names the first five by id; a store that cannot supply a total is counted page by page.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr

from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage, OffsetPageResponse
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.storage.sqlite import SqliteStorageProvider
from primer.workspace import template_in_use
from primer.workspace.template_in_use import MAX_WORKSPACE_IDS_SHOWN, TEMPLATE_IN_USE, TemplateInUse, workspaces_made_from


@pytest.fixture
async def store(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    yield sp.get_storage(Workspace)
    await sp.aclose()


def _workspace(workspace_id: str, template_id: str, provider_id: str = "p-1") -> Workspace:
    return Workspace(
        id=workspace_id, template_id=template_id, provider_id=provider_id, created_at=datetime.now(timezone.utc), phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959/", token=SecretStr("t")),
    )


async def _seed(store, template_id: str, ids: list[str], provider_id: str = "p-1") -> None:
    for workspace_id in ids:
        await store.create(_workspace(workspace_id, template_id, provider_id))


def test_the_constants_are_the_rulings() -> None:
    assert TEMPLATE_IN_USE == "template_in_use"
    assert MAX_WORKSPACE_IDS_SHOWN == 5


@pytest.mark.asyncio
async def test_no_workspace_made_from_it_is_none(store) -> None:
    await _seed(store, "other", ["ws-a"])

    assert await workspaces_made_from(store, "tpl-1") is None


@pytest.mark.asyncio
async def test_one_workspace_is_counted_and_named(store) -> None:
    await _seed(store, "tpl-1", ["ws-a"])
    await _seed(store, "other", ["ws-z"])

    answer = await workspaces_made_from(store, "tpl-1")

    assert answer == TemplateInUse(template_id="tpl-1", count=1, workspace_ids=("ws-a",))
    assert answer.message == "Workspace template 'tpl-1' is still used by 1 workspace (ws-a). Delete the workspace made from it first."


@pytest.mark.asyncio
async def test_up_to_five_workspaces_are_all_named_in_id_order(store) -> None:
    await _seed(store, "tpl-1", ["ws-d", "ws-b", "ws-e", "ws-a", "ws-c"])

    answer = await workspaces_made_from(store, "tpl-1")

    assert answer.count == 5 and answer.workspace_ids == ("ws-a", "ws-b", "ws-c", "ws-d", "ws-e")
    assert answer.message == (
        "Workspace template 'tpl-1' is still used by 5 workspaces (ws-a, ws-b, ws-c, ws-d, ws-e). Delete the workspaces made from it first."
    )


@pytest.mark.asyncio
async def test_more_than_five_are_counted_and_only_the_first_five_are_named(store) -> None:
    await _seed(store, "tpl-1", [f"ws-{n}" for n in (9, 3, 7, 1, 8, 2, 6, 4, 5, 10, 11, 12)])

    answer = await workspaces_made_from(store, "tpl-1")

    assert answer.count == 12
    assert answer.workspace_ids == ("ws-1", "ws-10", "ws-11", "ws-12", "ws-2"), "the first five by id, as the store sorts them"
    assert answer.message == (
        "Workspace template 'tpl-1' is still used by 12 workspaces (first 5: ws-1, ws-10, ws-11, ws-12, ws-2). "
        "Delete the workspaces made from it first."
    )
    assert answer.detail == {
        "error": "template_in_use", "kind": "workspace_template", "count": 12,
        "workspace_ids": ["ws-1", "ws-10", "ws-11", "ws-12", "ws-2"], "message": answer.message,
    }


@pytest.mark.asyncio
async def test_the_provider_the_workspace_is_on_does_not_matter(store) -> None:
    """Every provider kind: the question is the workspace's ``template_id``, nothing about where it runs."""
    await _seed(store, "tpl-1", ["ws-local"], provider_id="local")
    await _seed(store, "tpl-1", ["ws-docker"], provider_id="docker-1")
    await _seed(store, "tpl-1", ["ws-k8s"], provider_id="k3s")

    answer = await workspaces_made_from(store, "tpl-1")

    assert answer.count == 3 and answer.workspace_ids == ("ws-docker", "ws-k8s", "ws-local")


@pytest.mark.asyncio
async def test_a_template_id_that_is_a_prefix_of_another_is_not_confused_with_it(store) -> None:
    await _seed(store, "tpl-10", ["ws-a"])

    assert await workspaces_made_from(store, "tpl-1") is None
    assert await workspaces_made_from(store, "tpl-1 ") is None


class _Row(BaseModel):
    """A page's items are models (``OffsetPageResponse`` validates them); only the id is read."""

    id: str


class _NoTotalStore:
    """A store whose pages carry no total (the storage contract allows a backend to say so), over a fixed list of workspace ids."""

    def __init__(self, ids: list[str]) -> None:
        self._ids = sorted(ids)
        self.asked: list[tuple[int, int]] = []

    async def find(self, predicate, page, *, order_by=None):
        assert isinstance(page, OffsetPage)
        self.asked.append((page.offset, page.length))
        rows = [_Row(id=i) for i in self._ids[page.offset : page.offset + page.length]]
        return OffsetPageResponse(offset=page.offset, length=len(rows), total=None, items=rows)


@pytest.mark.asyncio
async def test_a_store_without_a_total_is_counted_page_by_page(monkeypatch) -> None:
    monkeypatch.setattr(template_in_use, "_PAGE", 4)
    store = _NoTotalStore([f"ws-{n:02d}" for n in range(11)])

    answer = await workspaces_made_from(store, "tpl-1")

    assert answer.count == 11 and answer.workspace_ids == tuple(f"ws-{n:02d}" for n in range(5))
    assert store.asked == [(0, 5), (5, 4), (9, 4)], "the first five, then the rest in pages"


@pytest.mark.asyncio
async def test_a_store_without_a_total_and_fewer_than_five_rows_needs_one_query() -> None:
    store = _NoTotalStore(["ws-a", "ws-b"])

    answer = await workspaces_made_from(store, "tpl-1")

    assert answer.count == 2 and store.asked == [(0, 5)]


@pytest.mark.asyncio
async def test_exactly_five_rows_without_a_total_are_confirmed_with_one_more_query() -> None:
    store = _NoTotalStore([f"ws-{n}" for n in range(5)])

    answer = await workspaces_made_from(store, "tpl-1")

    assert answer.count == 5 and store.asked == [(0, 5), (5, 200)]
