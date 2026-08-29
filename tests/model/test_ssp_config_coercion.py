"""SemanticSearchProvider.config union resolution (platform wave P3 follow-up).

PgVectorScaleConfig extends PgVectorConfig's own base
(_PgVectorBaseConfig) purely by adding optional fields, so a config
dict carrying only the shared core connection fields validates equally
well as either class. Without _coerce_config_to_provider, Pydantic's
smart-union resolution on the bare
``PgVectorConfig | PgVectorScaleConfig | LanceConfig`` field picks
PgVectorConfig regardless of the sibling ``provider`` field, and a
pgvectorscale create with only the core fields then 422s against
_validate_config_matches. Mirrors LLMProvider._coerce_config_to_provider's
same fix for the identical openresponses/openchat ambiguity
(tests/model/test_provider_openchat.py::TestLLMProviderUnionAcceptsOpenChat).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.provider import (
    LanceConfig,
    PgVectorConfig,
    PgVectorScaleConfig,
    SemanticSearchProvider,
    SemanticSearchProviderType,
)


def _core_pg_fields() -> dict:
    return {
        "hostname": "db.invalid",
        "port": 5432,
        "username": "u",
        "password": "p",
        "database": "d",
    }


def test_pgvectorscale_with_only_core_fields_resolves_correctly() -> None:
    """The exact scenario that used to 422: a pgvectorscale row whose
    config dict carries none of pgvectorscale's own diskann fields."""
    row = SemanticSearchProvider.model_validate({
        "id": "ssp-1",
        "provider": "pgvectorscale",
        "config": _core_pg_fields(),
    })
    assert row.provider is SemanticSearchProviderType.PGVECTORSCALE
    assert isinstance(row.config, PgVectorScaleConfig)
    # A real PgVectorScaleConfig field, absent on PgVectorConfig -
    # proof this is really the scale variant, not a lucky attribute.
    assert row.config.enable_diskann is False


def test_pgvector_still_resolves_to_pgvector() -> None:
    row = SemanticSearchProvider.model_validate({
        "id": "ssp-2",
        "provider": "pgvector",
        "config": _core_pg_fields(),
    })
    assert isinstance(row.config, PgVectorConfig)
    assert not isinstance(row.config, PgVectorScaleConfig)


def test_lance_still_resolves_to_lance() -> None:
    row = SemanticSearchProvider.model_validate({
        "id": "ssp-3",
        "provider": "lance",
        "config": {"path": "/tmp/lance-store"},
    })
    assert isinstance(row.config, LanceConfig)


def test_pgvectorscale_with_its_own_field_still_resolves_correctly() -> None:
    """Setting a pgvectorscale-only field explicitly (the pre-fix
    workaround) must keep working identically now that it is no longer
    required."""
    row = SemanticSearchProvider.model_validate({
        "id": "ssp-4",
        "provider": "pgvectorscale",
        "config": {**_core_pg_fields(), "enable_diskann": True},
    })
    assert isinstance(row.config, PgVectorScaleConfig)
    assert row.config.enable_diskann is True


def test_json_round_trip_preserves_pgvectorscale_type() -> None:
    """Mirrors test_provider_openchat.py's TestLLMProviderUnionAcceptsOpenChat
    ::test_json_round_trip - dump to JSON, re-validate the raw dict,
    confirm the type survives the round trip."""
    row = SemanticSearchProvider.model_validate({
        "id": "ssp-5",
        "provider": "pgvectorscale",
        "config": _core_pg_fields(),
    })
    dumped = row.model_dump(mode="json")
    dumped["config"]["password"] = "p"  # SecretStr dumps masked
    roundtrip = SemanticSearchProvider.model_validate(dumped)
    assert isinstance(roundtrip.config, PgVectorScaleConfig)


def test_mismatched_provider_and_config_still_rejected() -> None:
    """The coercion only resolves ambiguous dicts - it must not paper
    over a genuinely wrong config shape (e.g. a lance path key sent
    with provider=pgvector)."""
    with pytest.raises(ValidationError):
        SemanticSearchProvider.model_validate({
            "id": "ssp-6",
            "provider": "pgvector",
            "config": {"path": "/tmp/lance-store"},
        })


def test_config_already_typed_is_left_alone() -> None:
    """Constructing directly with a real config instance (not a raw
    dict) bypasses the dict-shaped coercion branch entirely - it must
    still validate normally."""
    row = SemanticSearchProvider(
        id="ssp-7",
        provider=SemanticSearchProviderType.PGVECTORSCALE,
        config=PgVectorScaleConfig(**_core_pg_fields()),
    )
    assert isinstance(row.config, PgVectorScaleConfig)
