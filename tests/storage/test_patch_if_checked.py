"""``patch_if_checked``: the CAS drift tripwire.

A rejection that a Python re-evaluation of the same ``where`` says should have matched is not a race;
it must log ERROR and be counted, and the return value must stay ``None``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

import primer.observability.metrics as metrics
from primer.storage.cas import patch_if_checked
from tests.storage import _patch_scenarios as ps


def _drift_count() -> float:
    return metrics.storage_cas_drift_total.labels("PatchDoc")._value.get()


class _RefusesEverything:
    """Delegates to a real store but makes patch_if refuse: what a backend/Python disagreement looks like."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def patch_if(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.get(*args, **kwargs)


@pytest.mark.asyncio
async def test_an_applied_write_is_returned_and_raises_no_alarm(fake_storage_provider, caplog):
    store = fake_storage_provider.get_storage(ps.PatchDoc)
    await store.create(ps.PatchDoc(id="a", status="running"))
    before = _drift_count()
    with caplog.at_level(logging.ERROR, logger="primer.storage.cas"):
        out = await patch_if_checked(store, "a", {"status": "done"}, where={"status": ["running"]})
    assert out is not None and out.status == "done"
    assert _drift_count() == before and not caplog.records


@pytest.mark.asyncio
async def test_a_real_race_is_a_quiet_none(fake_storage_provider, caplog):
    store = fake_storage_provider.get_storage(ps.PatchDoc)
    await store.create(ps.PatchDoc(id="a", status="done"))     # someone else already moved it on
    before = _drift_count()
    with caplog.at_level(logging.ERROR, logger="primer.storage.cas"):
        out = await patch_if_checked(store, "a", {"status": "x"}, where={"status": ["running"]})
    assert out is None
    assert _drift_count() == before and not caplog.records


@pytest.mark.asyncio
async def test_a_missing_row_still_raises_not_found(fake_storage_provider):
    from primer.model.except_ import NotFoundError

    store = fake_storage_provider.get_storage(ps.PatchDoc)
    with pytest.raises(NotFoundError):
        await patch_if_checked(store, "nope", {"status": "x"}, where={"status": ["running"]})


@pytest.mark.asyncio
async def test_a_refusal_the_fresh_row_contradicts_is_logged_and_counted(fake_storage_provider, caplog):
    store = fake_storage_provider.get_storage(ps.PatchDoc)
    await store.create(ps.PatchDoc(id="a", status="running"))
    before = _drift_count()
    with caplog.at_level(logging.ERROR, logger="primer.storage.cas"):
        out = await patch_if_checked(
            _RefusesEverything(store), "a", {"status": "done"}, where={"status": ["running"]},
        )
    assert out is None
    assert _drift_count() == before + 1
    assert any("serialization drift" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_status_exclusion_in_where_keeps_an_ended_row_quiet(fake_storage_provider, caplog):
    """The ENDED exclusion belongs in `where` (the allowed statuses): a row that legitimately left
    eligibility then fails the Python check too, so a refusal is not mistaken for drift."""
    store = fake_storage_provider.get_storage(ps.PatchDoc)
    await store.create(ps.PatchDoc(id="a", status="ended"))
    before = _drift_count()
    with caplog.at_level(logging.ERROR, logger="primer.storage.cas"):
        out = await patch_if_checked(
            _RefusesEverything(store), "a", {"count": 1},
            where={"status": ["created", "running", "paused"]},
        )
    assert out is None
    assert _drift_count() == before and not caplog.records


@pytest.mark.asyncio
async def test_a_refusal_by_a_path_guard_the_fresh_row_agrees_with_is_a_quiet_none(fake_storage_provider, caplog):
    """A guard on a nested leaf (a ``where`` key that is a path) is re-evaluated on the leaf, not on a top-level field named by the tuple: the leaf
    is set, so ``[None]`` does not match and the refusal is the race it looks like (ticket 01a12606)."""
    store = fake_storage_provider.get_storage(ps.PatchDoc)
    await store.create(ps.PatchDoc(id="a", status="running", state={"ps": {"k": {"v": 1}}}))
    before = _drift_count()
    with caplog.at_level(logging.ERROR, logger="primer.storage.cas"):
        out = await patch_if_checked(
            _RefusesEverything(store), "a", None, where={"status": ["running"], ("state", "ps", "k"): [None]}, set_paths={("state", "ps", "k"): {"v": 2}},
        )
    assert out is None
    assert _drift_count() == before and not caplog.records


@pytest.mark.asyncio
async def test_the_alarm_names_a_path_guard_beside_a_field_guard(fake_storage_provider, caplog):
    store = fake_storage_provider.get_storage(ps.PatchDoc)
    await store.create(ps.PatchDoc(id="a", status="running", state={"ps": {}}))
    with caplog.at_level(logging.ERROR, logger="primer.storage.cas"):
        out = await patch_if_checked(
            _RefusesEverything(store), "a", None, where={"status": ["running"], ("state", "ps", "k"): [None]}, set_paths={("state", "ps", "k"): 1},
        )
    text = " ".join(r.getMessage() for r in caplog.records)
    assert out is None and "serialization drift" in text and "status" in text and "('state', 'ps', 'k')" in text


@pytest.mark.asyncio
async def test_the_alarm_names_the_guarded_fields_and_never_their_values(fake_storage_provider, caplog):
    from pydantic import SecretStr

    store = fake_storage_provider.get_storage(ps.PatchDoc)
    await store.create(ps.PatchDoc(id="a", secret=SecretStr("hunter2-plaintext"), status="running"))
    with caplog.at_level(logging.ERROR, logger="primer.storage.cas"):
        await patch_if_checked(
            _RefusesEverything(store), "a", {"count": 1},
            where={"secret": ["hunter2-plaintext"], "status": ["running"]},
        )
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "secret" in text and "status" in text
    assert "hunter2-plaintext" not in text
