"""Top-level shared fixtures available to all test sub-packages.

The ``fake_storage_provider`` fixture is defined here so both
``tests/api/`` and ``tests/storage/`` (and any future package) can
use it without duplication.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

import pytest

from matrix.model.common import Identifiable
from matrix.model.except_ import ConflictError, NotFoundError
from matrix.model.storage import (
    CursorPage,
    CursorPageResponse,
    FieldRef,
    OffsetPage,
    OffsetPageResponse,
    Op,
    Predicate,
    Value,
)


_T = TypeVar("_T", bound=Identifiable)


class _InMemoryStorage(Generic[_T]):
    """Bare-bones in-memory ``Storage[T]`` for tests."""

    def __init__(self, model_cls: type[_T]) -> None:
        self._cls = model_cls
        self._data: dict[str, _T] = {}

    async def get(self, id: str) -> _T | None:
        return self._data.get(id)

    async def create(self, entity: _T) -> _T:
        if entity.id in self._data:
            raise ConflictError(f"id {entity.id!r} already exists")
        self._data[entity.id] = entity
        return entity

    async def update(self, entity: _T) -> _T:
        if entity.id not in self._data:
            raise NotFoundError(f"no entity with id {entity.id!r}")
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page, *, order_by=None):
        items = list(self._data.values())
        if isinstance(page, OffsetPage):
            sliced = items[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        offset = int(page.cursor) if page.cursor else 0
        sliced = items[offset : offset + page.length]
        next_cursor: str | None = None
        if offset + page.length < len(items):
            next_cursor = str(offset + page.length)
        return CursorPageResponse(next_cursor=next_cursor, items=sliced)

    async def find(self, predicate, page, *, order_by=None):
        if predicate is None:
            return await self.list(page, order_by=order_by)
        items = [e for e in self._data.values() if _eval_predicate(e, predicate)]
        if isinstance(page, OffsetPage):
            sliced = items[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        offset = int(page.cursor) if page.cursor else 0
        sliced = items[offset : offset + page.length]
        next_cursor: str | None = None
        if offset + page.length < len(items):
            next_cursor = str(offset + page.length)
        return CursorPageResponse(next_cursor=next_cursor, items=sliced)


def _resolve_field(entity: Any, path: str) -> Any:
    """Walk a dotted path against a Pydantic model / dict.

    Supports ``id``, ``status``, ``binding.agent_id`` -- the patterns the
    sessions router emits. Returns ``None`` for any unresolvable segment
    so a missing field naturally fails an ``EQ`` comparison.
    """
    cur: Any = entity
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    # Coerce Enums to their string value so EQ against a Value(value=str)
    # behaves like the Postgres translator (which compares text).
    from enum import Enum
    if isinstance(cur, Enum):
        return cur.value
    return cur


def _eval_predicate(entity: Any, node: Any) -> bool:
    """Tiny predicate evaluator for the in-memory test storage.

    Supports EQ / NE / AND / OR -- the operators the API routers
    actually emit when translating query params. Other operators fall
    through to ``True`` (the test storage is intentionally minimal).
    """
    if isinstance(node, Predicate):
        if node.op in (Op.AND, Op.OR):
            left = _eval_predicate(entity, node.left)
            right = _eval_predicate(entity, node.right)
            return (left and right) if node.op == Op.AND else (left or right)
        # Comparison: left is a FieldRef, right is a Value.
        if isinstance(node.left, FieldRef) and isinstance(node.right, Value):
            actual = _resolve_field(entity, node.left.name)
            expected = node.right.value
            if node.op == Op.EQ:
                return actual == expected
            if node.op == Op.NE:
                return actual != expected
            if node.op == Op.GT:
                return actual is not None and actual > expected
            if node.op == Op.LT:
                return actual is not None and actual < expected
            if node.op == Op.GE:
                return actual is not None and actual >= expected
            if node.op == Op.LE:
                return actual is not None and actual <= expected
        return True
    return True


class _FakeStorageProvider:
    """In-memory ``StorageProvider`` returning ``_InMemoryStorage`` per model."""

    def __init__(self) -> None:
        self._stores: dict[type, _InMemoryStorage[Any]] = {}

    def get_storage(self, model_class: type[_T]) -> _InMemoryStorage[_T]:
        return self._stores.setdefault(model_class, _InMemoryStorage(model_class))

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return


@pytest.fixture
def fake_storage_provider() -> _FakeStorageProvider:
    return _FakeStorageProvider()


@pytest.fixture
def fake_provider_registry(
    fake_storage_provider: _FakeStorageProvider,
) -> Any:
    """Minimal ProviderRegistry shim for tests outside tests/api/.

    The llm_factory and other factories are stubs; tests that need a
    real LLM should monkey-patch ``registry.get_llm`` directly (the
    ``deps`` fixture in tests/chat/test_dispatch.py does this).
    """
    from matrix.api.registries import ProviderRegistry

    return ProviderRegistry(
        fake_storage_provider,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )


__all__ = [
    "_FakeStorageProvider",
    "_InMemoryStorage",
]
