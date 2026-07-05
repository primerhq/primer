"""Unit tests for the PrincipalRef persisted projection (Layer 3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.principal import Principal, PrincipalRef


def test_from_principal_projects_five_fields() -> None:
    p = Principal(
        type="user", id="user-1", display="alice", role="admin", source="local",
    )
    ref = PrincipalRef.from_principal(p)
    assert ref.type == "user"
    assert ref.id == "user-1"
    assert ref.display == "alice"
    assert ref.role == "admin"
    assert ref.source == "local"


def test_system_fallback() -> None:
    ref = PrincipalRef.system()
    assert ref.type == "system"
    assert ref.id == "system"
    assert ref.display == "system"
    assert ref.role is None
    assert ref.source == "internal"


def test_frozen() -> None:
    ref = PrincipalRef.system()
    with pytest.raises(ValidationError):
        ref.id = "mutated"  # type: ignore[misc]


def test_roundtrips_through_json() -> None:
    ref = PrincipalRef(
        type="trigger", id="trig-1", display="nightly", role=None,
        source="internal",
    )
    assert PrincipalRef.model_validate(ref.model_dump(mode="json")) == ref
