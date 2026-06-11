from primer.model.provider import PgVectorConfig, PgVectorScaleConfig


def _common():
    return dict(hostname="h", port=5432, username="u", password="p", database="d")


def test_use_halfvec_defaults_false_on_both_configs():
    assert PgVectorConfig(**_common()).use_halfvec is False
    assert PgVectorScaleConfig(**_common()).use_halfvec is False


def test_use_halfvec_can_be_enabled():
    assert PgVectorConfig(**_common(), use_halfvec=True).use_halfvec is True
