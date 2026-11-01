"""KN-18: a pgvector store whose cached catalogue entry went stale (another process dropped and re-created the collection, perhaps at another width)
evicts it and resolves once more instead of failing until restart."""

from __future__ import annotations

import asyncpg
import pytest

from primer.model.except_ import BadRequestError, ProviderError, ServerError
from primer.vector.pgvector import _DimensionMismatch, _recreated_elsewhere


class _Store:
    def __init__(self, failures):
        self._collections = {"c": object(), "other": object()}
        self.failures = list(failures)
        self.calls = 0

    @_recreated_elsewhere
    async def search(self, collection_id, vector, k):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "ok"

    @_recreated_elsewhere
    async def put_many(self, records):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "ok"


async def test_a_width_mismatch_evicts_the_entry_and_retries_once():
    store = _Store([_DimensionMismatch("vector dimensionality 4 does not match")])
    assert await store.search("c", [0.0], 1) == "ok"
    assert store.calls == 2 and "c" not in store._collections and "other" in store._collections


async def test_a_missing_table_evicts_the_entry_and_retries_once():
    gone = ServerError("pgvector error: relation does not exist", cause=asyncpg.UndefinedTableError("relation does not exist"))
    store = _Store([gone])
    assert await store.search("c", [0.0], 1) == "ok"
    assert store.calls == 2


async def test_a_caller_who_is_really_wrong_gets_the_error_after_one_retry():
    store = _Store([_DimensionMismatch("a"), _DimensionMismatch("b")])
    with pytest.raises(_DimensionMismatch):
        await store.search("c", [0.0], 1)
    assert store.calls == 2


@pytest.mark.parametrize("failure", [BadRequestError("collection 'c' has not been created"), ProviderError("backend error"), ServerError("other")])
async def test_other_errors_are_not_retried(failure):
    store = _Store([failure])
    with pytest.raises(type(failure)):
        await store.search("c", [0.0], 1)
    assert store.calls == 1


async def test_a_batch_evicts_every_collection_it_names():
    from primer.model.vector import EmbeddingRecord

    records = [EmbeddingRecord(collection_id=c, document_id="d", chunk_id="0", text="t", vector=[0.0]) for c in ("c", "other")]
    store = _Store([_DimensionMismatch("x")])
    assert await store.put_many(records) == "ok"
    assert store._collections == {}
