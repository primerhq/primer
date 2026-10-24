"""The four ``system`` tools that read and set where web searches and page fetches go (platform revamp 04, Task 2).

``get_web_search_active_config`` / ``set_web_search_active_config`` and ``get_web_fetch_active_config`` /
``set_web_fetch_active_config`` are the tool twins of ``GET`` / ``PUT /v1/web_search_active_config`` and
``/v1/web_fetch_active_config``: admin-only like the routes, and a set runs the route's own check and write
(``primer/web_search/active_config.py``, ``primer/web_fetch/active_config.py``), so it refuses what the route refuses, in the
route's words, and stores nothing then. A get also lists the provider ids a set may name.

The set tools do not drop the services' active-config cache (it is local to one process; a tool may run in another), so their
descriptions promise the change within 5 seconds: the last test ties that promise to the services' cache TTL.

Run through the real system toolset on a real sqlite store (the ``world`` fixture of ``test_system_crud_guards``), seeded as the
lifespan seeds it.
"""

from __future__ import annotations

import inspect
import random

import pytest

from primer.api._app_bootstrap import _bootstrap_web_fetch, _bootstrap_web_search
from primer.mcp.safety import is_exposable
from primer.model.storage import OffsetPage
from primer.model.web_fetch import ACTIVE_WEB_FETCH_CONFIG_ID, ActiveWebFetchConfig, JinaFetchConfig, WebFetchProvider
from primer.model.web_search import ACTIVE_WEB_SEARCH_CONFIG_ID, ActiveWebSearchConfig, DuckDuckGoConfig, WebSearchProvider
from primer.web_fetch.service import WebFetchService
from primer.web_search.service import WebSearchService
from tests.toolset._desc_conformance import assert_tool_conforms
from tests.toolset.test_system_crud_guards import _call, world  # noqa: F401  (world is a fixture)

WEB_TOOLS = [
    "get_web_search_active_config",
    "set_web_search_active_config",
    "get_web_fetch_active_config",
    "set_web_fetch_active_config",
]


async def test_the_four_tools_are_admin_tools_that_conform(world) -> None:
    _, toolset, _ = world
    tools = {t.id: t async for t in toolset.list_tools()}

    for tid in WEB_TOOLS:
        assert_tool_conforms(tools[tid])
        assert toolset.required_role(tid) == "admin"
        assert is_exposable(tools[tid], provider=toolset) == (True, None), f"{tid} is not reachable over POST /v1/mcp"
    for tid in ("set_web_search_active_config", "set_web_fetch_active_config"):
        assert tools[tid].preview_args == ("config",)
        assert "5 seconds" in tools[tid].description and "validation-error" in tools[tid].description


async def test_get_returns_the_config_and_the_providers_it_may_name(world) -> None:
    sp, toolset, _ = world
    await _bootstrap_web_search(sp)

    is_error, body = await _call(toolset, "get_web_search_active_config")

    assert not is_error, body
    assert body == {
        "config": {"mode": "single", "provider_id": "DuckDuckGo"},
        "providers": [{"id": "DuckDuckGo", "provider_type": "duckduckgo"}],
    }


async def test_set_stores_an_ordered_list_without_duplicates(world) -> None:
    sp, toolset, _ = world
    await _bootstrap_web_fetch(sp)
    await sp.get_storage(WebFetchProvider).create(WebFetchProvider(id="jina-1", provider_type="jina", config=JinaFetchConfig()))

    is_error, body = await _call(
        toolset, "set_web_fetch_active_config", config={"mode": "aggregated", "provider_ids": ["jina-1", "local", "jina-1"]},
    )

    assert not is_error, body
    assert body == {"config": {"mode": "aggregated", "provider_ids": ["jina-1", "local"]}}
    stored = await sp.get_storage(ActiveWebFetchConfig).get(ACTIVE_WEB_FETCH_CONFIG_ID)
    assert stored.config.provider_ids == ["jina-1", "local"]


async def test_set_refuses_an_unknown_id_with_the_rest_routes_words_and_stores_nothing(world) -> None:
    sp, toolset, _ = world
    await _bootstrap_web_search(sp)

    is_error, body = await _call(toolset, "set_web_search_active_config", config={"mode": "single", "provider_id": "nope"})

    assert is_error and body == {
        "type": "validation-error",
        "message": "active config references unknown provider id(s): ['nope']",
    }
    stored = await sp.get_storage(ActiveWebSearchConfig).get(ACTIVE_WEB_SEARCH_CONFIG_ID)
    assert stored.config.provider_id == "DuckDuckGo"


async def test_set_refuses_an_empty_list_and_stores_nothing(world) -> None:
    sp, toolset, _ = world
    await _bootstrap_web_search(sp)

    is_error, body = await _call(toolset, "set_web_search_active_config", config={"mode": "aggregated", "provider_ids": []})

    assert is_error and body["type"] == "validation-error"
    assert body["message"].startswith("argument validation failed")
    stored = await sp.get_storage(ActiveWebSearchConfig).get(ACTIVE_WEB_SEARCH_CONFIG_ID)
    assert stored.config.provider_id == "DuckDuckGo"


async def test_get_says_unavailable_when_the_row_is_missing(world) -> None:
    _, toolset, _ = world  # nothing seeded

    is_error, body = await _call(toolset, "get_web_fetch_active_config")

    assert is_error and body["type"] == "unavailable" and "missing" in body["message"]


def test_the_five_seconds_the_descriptions_promise_is_the_services_cache_ttl() -> None:
    for cls in (WebSearchService, WebFetchService):
        assert inspect.signature(cls.__init__).parameters["cache_ttl_seconds"].default == 5.0


@pytest.mark.parametrize("setting", ["web_search", "web_fetch"])
async def test_get_lists_the_first_200_providers_by_id_in_order(world, setting) -> None:  # audit G3-06
    """The get tools promise the providers a set may name as "the first 200 provider rows by id". More than 200 rows, stored in a shuffled order,
    come back as the 200 smallest ids in id order: dropping the order (the rows come back in the order they were stored) or the cap fails here."""
    sp, toolset, _ = world
    if setting == "web_search":
        await _bootstrap_web_search(sp)
        model, make = WebSearchProvider, lambda pid: WebSearchProvider(id=pid, provider_type="duckduckgo", config=DuckDuckGoConfig())
    else:
        await _bootstrap_web_fetch(sp)
        model, make = WebFetchProvider, lambda pid: WebFetchProvider(id=pid, provider_type="jina", config=JinaFetchConfig())
    bootstrapped = [row.id for row in (await sp.get_storage(model).list(OffsetPage(offset=0, length=200))).items]
    ids = [f"p-{n:03d}" for n in range(230)]
    stored_order = ids[:]
    random.Random(3).shuffle(stored_order)
    for pid in stored_order:
        await sp.get_storage(model).create(make(pid))

    is_error, body = await _call(toolset, f"get_{setting}_active_config")

    assert not is_error, body
    listed = [p["id"] for p in body["providers"]]
    assert len(listed) == 200, "the cap"
    assert listed == sorted(ids + bootstrapped)[:200], "the first 200 by id, in id order"
