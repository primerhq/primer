"""Unit tests for the CDC kind registry in :mod:`primer.api.routers._cdc_hooks`."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from primer.api.routers._cdc_hooks import (
    _reset_for_test,
    known_cdc_kinds,
    register_cdc_kind,
)


# ---------------------------------------------------------------------------
# Minimal stub models used as stand-ins for real entity classes.
# ---------------------------------------------------------------------------


class SomeModel(BaseModel):
    id: str = ""


class OtherModel(BaseModel):
    id: str = ""


# ---------------------------------------------------------------------------
# Fixture: reset the registry after every test so state doesn't bleed.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry() -> None:  # type: ignore[return]
    """Ensure each test starts with an empty registry."""
    _reset_for_test()
    yield
    _reset_for_test()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_register_cdc_kind_adds_to_registry() -> None:
    register_cdc_kind("foo", SomeModel)
    assert "foo" in known_cdc_kinds()


def test_known_cdc_kinds_returns_copy() -> None:
    """Mutating the returned dict must not affect the real registry."""
    register_cdc_kind("foo", SomeModel)
    snapshot = known_cdc_kinds()
    snapshot["bar"] = OtherModel  # mutate the copy
    assert "bar" not in known_cdc_kinds()


def test_duplicate_registration_raises() -> None:
    register_cdc_kind("foo", SomeModel)
    with pytest.raises(ValueError, match="foo"):
        register_cdc_kind("foo", OtherModel)


def test_idempotent_with_same_model() -> None:
    register_cdc_kind("foo", SomeModel)
    register_cdc_kind("foo", SomeModel)  # no-op — same class
    assert known_cdc_kinds()["foo"] is SomeModel


def test_multiple_kinds_coexist() -> None:
    register_cdc_kind("foo", SomeModel)
    register_cdc_kind("bar", OtherModel)
    kinds = known_cdc_kinds()
    assert kinds["foo"] is SomeModel
    assert kinds["bar"] is OtherModel


def test_reset_clears_registry() -> None:
    register_cdc_kind("foo", SomeModel)
    _reset_for_test()
    assert "foo" not in known_cdc_kinds()
