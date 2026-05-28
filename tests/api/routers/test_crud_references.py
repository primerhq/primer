"""Unit tests for :mod:`matrix.api.routers._references`.

These tests exercise ``ReferenceCheck`` and ``build_reference_block_hook``
in isolation — no router, no app, no HTTP client required.  The fake
storage and fake request stubs are declared locally so the tests remain
self-contained.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from matrix.api.routers._references import ReferenceCheck, build_reference_block_hook
from matrix.model.storage import OffsetPage, OffsetPageResponse


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
