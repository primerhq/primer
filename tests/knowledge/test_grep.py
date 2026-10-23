"""grep_collection: regex line scan over content-store bodies."""
from __future__ import annotations

import pytest
import pytest_asyncio

from primer.knowledge.grep import grep_collection
from primer.model.except_ import BadRequestError
from primer.model.provider import (
    SqliteConfig, StorageProviderConfig, StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory


@pytest_asyncio.fixture
async def store(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    cs = provider.get_content_store()
    await cs.ensure_schema()
    await cs.upsert(document_id="d1", collection_id="c1", path="a",
                    content="alpha\nneedle here\nomega")
    await cs.upsert(document_id="d2", collection_id="c1", path="sub/b",
                    content="no match")
    await cs.upsert(document_id="d3", collection_id="c1", path="sub/c",
                    content="needle again")
    yield cs
    await provider.aclose()


async def test_hits_carry_path_line_excerpt(store):
    res = await grep_collection(store, collection_id="c1", pattern="needle")
    assert [(h.path, h.line) for h in res.hits] == [("a", 2), ("sub/c", 1)]
    assert res.truncated is False


async def test_path_prefix_filters(store):
    res = await grep_collection(
        store, collection_id="c1", pattern="needle", path_prefix="sub/",
    )
    assert [h.path for h in res.hits] == ["sub/c"]


async def test_cap_sets_truncated(store):
    res = await grep_collection(store, collection_id="c1", pattern=".", max_results=1)
    assert len(res.hits) == 1 and res.truncated is True


async def test_bad_regex_is_bad_request(store):
    with pytest.raises(BadRequestError):
        await grep_collection(store, collection_id="c1", pattern="(unclosed")


# ---- G6-4: the pattern is bounded in size and time, and matched off the event loop -------------------------------------

HOSTILE = r"(a|aa)+$"  # exponential in both engines; about 5 s under stdlib re on this line
HOSTILE_LINE = "a" * 36 + "!"


async def _max_loop_gap(body) -> tuple[float, object]:
    import asyncio
    import time

    gaps = [0.0]
    state = {"stop": False}

    async def ticker():
        last = time.perf_counter()
        while not state["stop"]:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    try:
        outcome = await body()
    except Exception as exc:  # noqa: BLE001 - returned for the caller to assert on
        outcome = exc
    finally:
        state["stop"] = True
        await task
    return max(gaps), outcome


async def test_a_catastrophic_pattern_is_a_bad_request_and_the_loop_keeps_running(store, monkeypatch):
    import time

    import primer.knowledge.grep as grep_module

    monkeypatch.setattr(grep_module, "GREP_MATCH_SECONDS", 0.3, raising=False)
    await store.upsert(document_id="d9", collection_id="c1", path="hostile", content=f"ok\n{HOSTILE_LINE}")
    started = time.perf_counter()
    gap, outcome = await _max_loop_gap(
        lambda: grep_collection(store, collection_id="c1", pattern=HOSTILE),
    )
    assert isinstance(outcome, BadRequestError), outcome
    assert "matching budget" in str(outcome)
    assert time.perf_counter() - started < 2.0
    assert gap < 0.2, gap


async def test_a_pattern_over_1000_characters_is_a_bad_request(store):
    with pytest.raises(BadRequestError, match="1001 characters long; the limit is 1000"):
        await grep_collection(store, collection_id="c1", pattern="n" * 1001)


async def test_a_literal_search_is_a_substring_test_with_no_pattern_cap(store):
    long_line = "x(" * 700
    await store.upsert(document_id="d8", collection_id="c1", path="long", content=f"head\n{long_line}tail")
    res = await grep_collection(store, collection_id="c1", pattern=long_line, literal=True)
    assert [(h.path, h.line) for h in res.hits] == [("long", 2)]
    res = await grep_collection(store, collection_id="c1", pattern="needle", literal=True, max_results=1)
    assert [(h.path, h.line) for h in res.hits] == [("a", 2)] and res.truncated is True
