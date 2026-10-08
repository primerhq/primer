"""The graph walk behind an agent's and a graph's delete block survives a row it cannot read and a row deleted mid-walk (A-09, review).

A graph names an agent or another graph through its ``nodes`` list, which the predicate language cannot match into, so
``primer.storage.references`` reads every graph a page at a time. Two things the first version did not survive, on a real SQLite
store (the in-memory fakes never decode a row, and their cursor is an offset):

* a graph row the model cannot decode (a shape that drifted, a hand edit) made the page that holds it raise, so EVERY agent and graph
  delete failed with a validation error, whatever it was asked to delete. Now the walk steps through that page one row at a time:
  readable rows are still checked, and the unreadable one is a blocker (the safe direction for a delete guard) that is named by its
  position, because a row that cannot be decoded has no id the typed API can return: it follows the last readable graph;
* the walk paged by offset, so a graph deleted between two pages moved every later row up one place and the page boundary skipped a
  row, which could be the one that names the parent. It now walks by cursor.
"""

from __future__ import annotations

import pytest

from primer.model.graph import Graph
from primer.storage import references
from tests._support.compute_rows import graph_naming_agent, graph_row, insert_unreadable_graph


async def _seed(provider, row) -> None:
    await provider.get_storage(type(row)).create(row)


@pytest.mark.asyncio
async def test_a_referencing_graph_is_still_found_when_an_unreadable_row_shares_its_page(sqlite_provider) -> None:
    await _seed(sqlite_provider, graph_naming_agent("g-1", "ag-1"))
    await insert_unreadable_graph(sqlite_provider, "g-9")

    found = await references.first_graph_with_agent_node(sqlite_provider.get_storage(Graph), "ag-1")

    assert found is not None and found.id == "g-1"


@pytest.mark.asyncio
async def test_an_unreadable_row_is_a_blocker_named_by_the_graph_before_it(sqlite_provider) -> None:
    await _seed(sqlite_provider, graph_row("g-1"))
    await insert_unreadable_graph(sqlite_provider, "g-9")

    found = await references.first_graph_with_agent_node(sqlite_provider.get_storage(Graph), "ag-1")

    assert found is not None
    assert "unreadable" in found.id and "g-1" in found.id, found.id


@pytest.mark.asyncio
async def test_an_unreadable_first_row_is_a_blocker_that_says_it_is_the_first(sqlite_provider) -> None:
    await insert_unreadable_graph(sqlite_provider, "a-0")
    await _seed(sqlite_provider, graph_row("g-1"))

    found = await references.first_graph_with_subgraph_node(sqlite_provider.get_storage(Graph), "g-2")

    assert found is not None
    assert "unreadable" in found.id and "first" in found.id, found.id


@pytest.mark.asyncio
async def test_an_unreadable_row_on_a_later_page_is_found_after_the_readable_pages(sqlite_provider, monkeypatch) -> None:
    monkeypatch.setattr(references, "_PAGE", 2)
    for graph_id in ("g-1", "g-2", "g-3"):
        await _seed(sqlite_provider, graph_row(graph_id))
    await insert_unreadable_graph(sqlite_provider, "g-4")

    found = await references.first_graph_with_agent_node(sqlite_provider.get_storage(Graph), "ag-1")

    assert found is not None
    assert "unreadable" in found.id and "g-3" in found.id, found.id


@pytest.mark.asyncio
async def test_nothing_is_reported_when_every_row_is_readable_and_none_names_the_parent(sqlite_provider, monkeypatch) -> None:
    monkeypatch.setattr(references, "_PAGE", 2)
    for graph_id in ("g-1", "g-2", "g-3"):
        await _seed(sqlite_provider, graph_row(graph_id))

    assert await references.first_graph_with_agent_node(sqlite_provider.get_storage(Graph), "ag-1") is None


@pytest.mark.asyncio
async def test_a_graph_deleted_between_two_pages_does_not_hide_the_one_that_names_the_agent(sqlite_provider, monkeypatch) -> None:
    """Pages of two: [g-1, g-2] then [g-3, g-4]. g-1 is deleted after the first page is read. An offset walk asks for offset 2 of
    [g-2, g-3, g-4] and so never sees g-3, the graph that names the agent. A cursor walk resumes after g-2."""
    monkeypatch.setattr(references, "_PAGE", 2)
    await _seed(sqlite_provider, graph_row("g-1"))
    await _seed(sqlite_provider, graph_row("g-2"))
    await _seed(sqlite_provider, graph_naming_agent("g-3", "ag-1"))
    await _seed(sqlite_provider, graph_row("g-4"))
    storage = sqlite_provider.get_storage(Graph)
    real_list = storage.list
    calls = {"n": 0}

    async def list_then_delete_g1(page, **kwargs):
        result = await real_list(page, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            await storage.delete("g-1")
        return result

    monkeypatch.setattr(storage, "list", list_then_delete_g1)

    found = await references.first_graph_with_agent_node(storage, "ag-1")

    assert found is not None and found.id == "g-3", found
