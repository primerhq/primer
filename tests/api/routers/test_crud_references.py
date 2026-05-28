"""Tests for :mod:`matrix.api.routers._references`.

Unit tests exercise ``ReferenceCheck`` and ``build_reference_block_hook``
in isolation.  Integration tests exercise the ``references=`` parameter on
:func:`make_crud_router` end-to-end via an in-memory ASGI client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport
from pydantic import BaseModel

from matrix.api.errors import register_error_handlers
from matrix.api.routers._crud import make_crud_router
from matrix.api.routers._references import ReferenceCheck, build_reference_block_hook
from matrix.model.storage import FieldRef, OffsetPage, OffsetPageResponse, Op, Predicate, Value


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class _Parent(BaseModel):
    id: str


class _Child(BaseModel):
    id: str
    parent_id: str


# ---------------------------------------------------------------------------
# Fake helpers
# ---------------------------------------------------------------------------


class _FakeStorage:
    """Minimal storage that filters items by a predicate's EQ value."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    async def find(self, predicate: Any, page: OffsetPage, **_: Any) -> OffsetPageResponse[Any]:  # type: ignore[override]
        # Evaluate the simple EQ predicate manually.
        from matrix.model.storage import FieldRef, Op, Predicate, Value

        matched: list[Any] = []
        if isinstance(predicate, Predicate) and predicate.op == Op.EQ:
            left = predicate.left
            right = predicate.right
            if isinstance(left, FieldRef) and isinstance(right, Value):
                field_name = left.name
                expected = right.value
                for item in self._items:
                    actual = getattr(item, field_name, None)
                    if actual == expected:
                        matched.append(item)

        sliced = matched[page.offset : page.offset + page.length]
        return OffsetPageResponse(
            offset=page.offset,
            length=len(sliced),
            total=len(matched),
            items=sliced,
        )


def _fake_request() -> MagicMock:
    """Return a minimal fake ``Request`` object."""
    return MagicMock()


# ---------------------------------------------------------------------------
# Tests: hook blocks delete when child exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reference_check_blocks_when_child_exists() -> None:
    child = _Child(id="c1", parent_id="p1")
    fake_storage = _FakeStorage([child])
    check = ReferenceCheck(
        child_kind="child",
        child_storage=lambda req: fake_storage,
        child_field="parent_id",
    )
    hook = build_reference_block_hook([check])
    parent = _Parent(id="p1")

    with pytest.raises(HTTPException) as exc_info:
        await hook(parent, _fake_request())

    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert detail["error"] == "in_use_by"
    assert detail["child_kind"] == "child"
    assert detail["count"] >= 1


@pytest.mark.asyncio
async def test_reference_check_allows_when_no_child() -> None:
    fake_storage = _FakeStorage([])
    check = ReferenceCheck(
        child_kind="child",
        child_storage=lambda req: fake_storage,
        child_field="parent_id",
    )
    hook = build_reference_block_hook([check])
    parent = _Parent(id="p1")

    # No exception should be raised.
    await hook(parent, _fake_request())


# ---------------------------------------------------------------------------
# Tests: error_code customisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reference_check_custom_error_code() -> None:
    child = _Child(id="c1", parent_id="p1")
    fake_storage = _FakeStorage([child])
    check = ReferenceCheck(
        child_kind="channel",
        child_storage=lambda req: fake_storage,
        child_field="parent_id",
        error_code="blocked_by_channel",
    )
    hook = build_reference_block_hook([check])
    parent = _Parent(id="p1")

    with pytest.raises(HTTPException) as exc_info:
        await hook(parent, _fake_request())

    assert exc_info.value.detail["error"] == "blocked_by_channel"


# ---------------------------------------------------------------------------
# Tests: stops at first failing check (short-circuit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_failing_check_short_circuits() -> None:
    """The hook raises on the first check that finds a child; later checks
    are not executed."""
    child_a = _Child(id="a1", parent_id="p1")
    storage_a = _FakeStorage([child_a])

    # Second storage would raise if called (proves short-circuit).
    class _NeverCalled:
        async def find(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("second check should not be called")

    check_a = ReferenceCheck(
        child_kind="alpha",
        child_storage=lambda req: storage_a,
        child_field="parent_id",
    )
    check_b = ReferenceCheck(
        child_kind="beta",
        child_storage=lambda req: _NeverCalled(),
        child_field="parent_id",
    )
    hook = build_reference_block_hook([check_a, check_b])
    parent = _Parent(id="p1")

    with pytest.raises(HTTPException) as exc_info:
        await hook(parent, _fake_request())

    assert exc_info.value.detail["child_kind"] == "alpha"


# ---------------------------------------------------------------------------
# Tests: multiple checks — all pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_checks_all_pass() -> None:
    storage_a = _FakeStorage([])
    storage_b = _FakeStorage([])
    check_a = ReferenceCheck(
        child_kind="alpha",
        child_storage=lambda req: storage_a,
        child_field="parent_id",
    )
    check_b = ReferenceCheck(
        child_kind="beta",
        child_storage=lambda req: storage_b,
        child_field="parent_id",
    )
    hook = build_reference_block_hook([check_a, check_b])
    parent = _Parent(id="p1")

    # No exception — all checks clear.
    await hook(parent, _fake_request())


# ---------------------------------------------------------------------------
# Tests: empty checks list is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_checks_is_noop() -> None:
    hook = build_reference_block_hook([])
    parent = _Parent(id="p1")

    # No exception expected.
    await hook(parent, _fake_request())


# ---------------------------------------------------------------------------
# Tests: ReferenceCheck is frozen (immutable)
# ---------------------------------------------------------------------------


def test_reference_check_is_frozen() -> None:
    check = ReferenceCheck(
        child_kind="channel",
        child_storage=lambda req: None,
        child_field="provider_id",
    )
    with pytest.raises((AttributeError, TypeError)):
        check.child_kind = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests: child belonging to different parent does not block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_of_different_parent_does_not_block() -> None:
    child = _Child(id="c1", parent_id="other-parent")
    fake_storage = _FakeStorage([child])
    check = ReferenceCheck(
        child_kind="child",
        child_storage=lambda req: fake_storage,
        child_field="parent_id",
    )
    hook = build_reference_block_hook([check])
    parent = _Parent(id="p1")

    # No exception — the child belongs to a different parent.
    await hook(parent, _fake_request())


# ===========================================================================
# Integration tests: make_crud_router references= parameter
# ===========================================================================


# ---------------------------------------------------------------------------
# Minimal models for integration tests
# ---------------------------------------------------------------------------


class _IParent(BaseModel):
    id: str


class _IChild(BaseModel):
    id: str
    parent_id: str


# ---------------------------------------------------------------------------
# In-memory storages for integration tests
# ---------------------------------------------------------------------------


class _IParentStorage:
    def __init__(self, items: list[_IParent] | None = None) -> None:
        self._data: dict[str, _IParent] = {item.id: item for item in (items or [])}

    async def get(self, id: str) -> _IParent | None:
        return self._data.get(id)

    async def create(self, entity: _IParent) -> _IParent:
        self._data[entity.id] = entity
        return entity

    async def update(self, entity: _IParent) -> _IParent:
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        self._data.pop(id, None)

    async def list(self, page: Any, *, order_by: Any = None) -> OffsetPageResponse[_IParent]:
        items = list(self._data.values())
        return OffsetPageResponse(offset=0, length=len(items), total=len(items), items=items)

    async def find(self, predicate: Any, page: Any, *, order_by: Any = None) -> OffsetPageResponse[_IParent]:
        return await self.list(page, order_by=order_by)


class _IChildStorage:
    def __init__(self, items: list[_IChild] | None = None) -> None:
        self._data: dict[str, _IChild] = {item.id: item for item in (items or [])}

    async def get(self, id: str) -> _IChild | None:
        return self._data.get(id)

    async def create(self, entity: _IChild) -> _IChild:
        self._data[entity.id] = entity
        return entity

    async def update(self, entity: _IChild) -> _IChild:
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        self._data.pop(id, None)

    async def list(self, page: Any, *, order_by: Any = None) -> OffsetPageResponse[_IChild]:
        items = list(self._data.values())
        return OffsetPageResponse(offset=0, length=len(items), total=len(items), items=items)

    async def find(self, predicate: Any, page: Any, *, order_by: Any = None) -> OffsetPageResponse[_IChild]:
        """Filter by a simple EQ predicate on parent_id."""
        matched: list[_IChild] = []
        if isinstance(predicate, Predicate) and predicate.op == Op.EQ:
            left = predicate.left
            right = predicate.right
            if isinstance(left, FieldRef) and isinstance(right, Value):
                for item in self._data.values():
                    if getattr(item, left.name, None) == right.value:
                        matched.append(item)
        else:
            matched = list(self._data.values())

        if isinstance(page, OffsetPage):
            sliced = matched[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(matched),
                items=sliced,
            )
        return OffsetPageResponse(offset=0, length=len(matched), total=len(matched), items=matched)


# ---------------------------------------------------------------------------
# Shared storage singletons + dependency factories
# ---------------------------------------------------------------------------

_PARENT_STORAGE: _IParentStorage | None = None
_CHILD_STORAGE: _IChildStorage | None = None


def _get_parent_storage() -> _IParentStorage:
    assert _PARENT_STORAGE is not None
    return _PARENT_STORAGE


def _get_child_storage() -> _IChildStorage:
    assert _CHILD_STORAGE is not None
    return _CHILD_STORAGE


# ---------------------------------------------------------------------------
# Fixture: references_client
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def references_client() -> AsyncIterator[httpx.AsyncClient]:
    global _PARENT_STORAGE, _CHILD_STORAGE  # noqa: PLW0603

    # Seed: two parents p1, p2; one child of p1 only.
    _PARENT_STORAGE = _IParentStorage(
        items=[_IParent(id="p1"), _IParent(id="p2")]
    )
    _CHILD_STORAGE = _IChildStorage(
        items=[_IChild(id="c1", parent_id="p1")]
    )

    parent_router = make_crud_router(
        model_cls=_IParent,
        storage_dep=_get_parent_storage,
        plural="parents",
        tag="parents",
        references=[
            ReferenceCheck(
                child_kind="child",
                child_storage=lambda req: _get_child_storage(),
                child_field="parent_id",
            )
        ],
    )
    child_router = make_crud_router(
        model_cls=_IChild,
        storage_dep=_get_child_storage,
        plural="children",
        tag="children",
    )

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(parent_router, prefix="/v1")
    app.include_router(child_router, prefix="/v1")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    _PARENT_STORAGE = None
    _CHILD_STORAGE = None


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reference_check_blocks_delete_when_child_exists(
    references_client: httpx.AsyncClient,
) -> None:
    """DELETE /v1/parents/p1 should return 409 because child c1 references p1."""
    resp = await references_client.delete("/v1/parents/p1")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "in_use_by"
    assert resp.json()["detail"]["child_kind"] == "child"


@pytest.mark.asyncio
async def test_reference_check_allows_delete_when_no_child(
    references_client: httpx.AsyncClient,
) -> None:
    """DELETE /v1/parents/p2 should succeed because p2 has no children."""
    resp = await references_client.delete("/v1/parents/p2")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_references_composes_with_user_on_pre_delete(
) -> None:
    """When both references= and on_pre_delete are set, the reference check
    fires first; on_pre_delete fires afterwards on success."""
    global _PARENT_STORAGE, _CHILD_STORAGE  # noqa: PLW0603

    _PARENT_STORAGE = _IParentStorage(items=[_IParent(id="solo")])
    _CHILD_STORAGE = _IChildStorage(items=[])

    hook_called: list[str] = []

    async def _user_hook(entity: Any, request: Any) -> None:
        hook_called.append(entity.id)

    parent_router = make_crud_router(
        model_cls=_IParent,
        storage_dep=_get_parent_storage,
        plural="parents2",
        tag="parents2",
        references=[
            ReferenceCheck(
                child_kind="child",
                child_storage=lambda req: _get_child_storage(),
                child_field="parent_id",
            )
        ],
        on_pre_delete=_user_hook,
    )

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(parent_router, prefix="/v1")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/v1/parents2/solo")

    assert resp.status_code == 204
    assert hook_called == ["solo"]

    _PARENT_STORAGE = None
    _CHILD_STORAGE = None
