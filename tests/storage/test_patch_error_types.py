"""``PatchSpecError`` is the caller's mistake; every OTHER ``ValueError`` under a ``patch_if`` write is a backend failure.

The backends used to re-raise ANY ``ValueError`` as itself (so that a malformed spec, a leaf the model drops and a pydantic
``ValidationError`` reach the caller unwrapped). That also re-raised a ``ValueError`` from a corrupt stored document or a
driver quirk as if the caller had made a mistake. The spec errors are now a distinct ``ValueError`` subclass, the backends
re-raise only that and ``ValidationError``, and anything else is wrapped like every other backend failure.

Each case runs on the real SQLite backend and on the Postgres backend through a scripted connection.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from pydantic import ValidationError

from primer.model.except_ import ProviderError
from primer.storage._patch import PatchSpecError, document_matches, json_equal
from primer.storage.postgres import PostgresStorage, _table_ensured
from tests.storage._patch_scenarios import StrictDoc
from tests.storage.test_patch_if_postgres_fixup import _Provider, _ScriptedConn, _row


def _postgres_storage() -> PostgresStorage[StrictDoc]:
    storage = PostgresStorage[StrictDoc](provider=_Provider(), model_class=StrictDoc)
    _table_ensured.add((id(storage._provider), StrictDoc))
    return storage


@pytest_asyncio.fixture
async def sqlite_storage(sqlite_provider):
    storage = sqlite_provider.get_storage(StrictDoc)
    await storage.create(StrictDoc(id="a"))
    return storage


MALFORMED = [
    dict(patch={}, where={"status": ["created"]}),                              # nothing to write
    dict(patch={"id": "b"}, where={"status": ["created"]}),                     # cannot change the id
    dict(patch={"nope": 1}, where={"status": ["created"]}),                     # a field the model does not have
    dict(patch={"count": 1}, where={}),                                         # no guard
    dict(patch={"count": 1}, where={"status": []}),                             # a guard that can never match
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", MALFORMED)
async def test_a_malformed_spec_is_a_patch_spec_error_on_both_backends(sqlite_storage, kwargs):
    for storage, conn in ((sqlite_storage, None), (_postgres_storage(), _ScriptedConn(_row()))):
        with pytest.raises(PatchSpecError) as excinfo:
            await storage.patch_if("a", **kwargs, **({} if conn is None else {"conn": conn}))
        assert isinstance(excinfo.value, ValueError), "existing handlers that catch ValueError keep working"


@pytest.mark.asyncio
async def test_a_value_error_that_is_not_a_spec_error_is_wrapped_on_sqlite(sqlite_storage, monkeypatch):
    """A corrupt stored document, say: ``_from_row`` raises a plain ValueError after the UPDATE returned a row."""
    def corrupt(*_a, **_k):
        raise ValueError("stored document is not valid JSON")

    monkeypatch.setattr(sqlite_storage, "_from_row", corrupt)
    with pytest.raises(ProviderError) as excinfo:
        await sqlite_storage.patch_if("a", {"count": 1}, where={"status": ["created"]})
    assert not isinstance(excinfo.value, ValueError) and isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_a_value_error_that_is_not_a_spec_error_is_wrapped_on_postgres(monkeypatch):
    storage = _postgres_storage()

    def corrupt(*_a, **_k):
        raise ValueError("stored document is not valid JSON")

    monkeypatch.setattr(storage, "_from_row", corrupt)
    with pytest.raises(ProviderError) as excinfo:
        await storage.patch_if("a", {"count": 1}, where={"status": ["created"]}, conn=_ScriptedConn(_row()))
    assert not isinstance(excinfo.value, ValueError) and isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_a_validation_error_is_still_reported_as_itself_on_both_backends(sqlite_storage):
    """A patch that leaves the row unreadable is the caller's mistake too (pydantic's ValidationError, a ValueError
    subclass that is NOT a PatchSpecError), and it is re-raised unwrapped."""
    with pytest.raises(ValidationError):
        await sqlite_storage.patch_if("a", {"count": "not a number"}, where={"status": ["created"]})
    conn = _ScriptedConn(_row(count="not a number"))
    with pytest.raises(ValidationError):
        await _postgres_storage().patch_if(
            "a", {"count": "not a number"}, where={"status": ["created"]}, conn=conn,
        )


def test_the_python_guard_check_uses_the_same_equality_as_where():
    """``document_matches`` (the drift tripwire's check) and ``where`` share ONE typed-equality helper, ``json_equal``: a
    private scalar-only copy of it was deleted."""
    assert document_matches({"n": 5}, {"n": [5.0]}) and json_equal(5, 5.0)
    assert not document_matches({"flag": True}, {"flag": [1]}) and not json_equal(True, 1)
    assert not document_matches({"a": None}, {"a": [0]})
    import primer.storage._patch as patch_module

    assert not hasattr(patch_module, "_typed_equal")
