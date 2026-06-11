import pytest

from primer.model.except_ import BadRequestError
from primer.model.provider import PgVectorConfig, PgVectorScaleConfig
from primer.vector.pgvector import (
    HALFVEC_MAX_DIMS,
    VECTOR_MAX_DIMS,
    _opclass_for,
    _validate_dimensions,
    _vec_to_list,
    _vector_column_type,
)


def _common():
    return dict(hostname="h", port=5432, username="u", password="p", database="d")


def test_use_halfvec_defaults_false_on_both_configs():
    assert PgVectorConfig(**_common()).use_halfvec is False
    assert PgVectorScaleConfig(**_common()).use_halfvec is False


def test_use_halfvec_can_be_enabled():
    assert PgVectorConfig(**_common(), use_halfvec=True).use_halfvec is True


def test_vector_column_type():
    assert _vector_column_type(False) == "vector"
    assert _vector_column_type(True) == "halfvec"


def test_opclass_for_vector_and_halfvec():
    assert _opclass_for("cosine", "vector") == "vector_cosine_ops"
    assert _opclass_for("l2", "vector") == "vector_l2_ops"
    assert _opclass_for("ip", "vector") == "vector_ip_ops"
    assert _opclass_for("cosine", "halfvec") == "halfvec_cosine_ops"
    assert _opclass_for("l2", "halfvec") == "halfvec_l2_ops"
    assert _opclass_for("ip", "halfvec") == "halfvec_ip_ops"


def test_validate_dimensions_ok_cases():
    assert _validate_dimensions(1536, use_halfvec=False) is None
    assert _validate_dimensions(2000, use_halfvec=False) is None
    assert _validate_dimensions(3072, use_halfvec=True) is None
    assert _validate_dimensions(4000, use_halfvec=True) is None


def test_validate_dimensions_over_vector_limit_tells_user_to_enable_halfvec():
    with pytest.raises(BadRequestError) as exc:
        _validate_dimensions(3072, use_halfvec=False)
    msg = str(exc.value)
    assert "2000" in msg and "use_halfvec" in msg


def test_validate_dimensions_over_halfvec_limit():
    with pytest.raises(BadRequestError) as exc:
        _validate_dimensions(4001, use_halfvec=True)
    assert "4000" in str(exc.value)


def test_vec_to_list_handles_list_and_to_list_objects():
    assert _vec_to_list([1.0, 2.0]) == [1.0, 2.0]
    assert _vec_to_list(None) == []

    class _HasToList:
        def to_list(self): return [3.0, 4.0]
    assert _vec_to_list(_HasToList()) == [3.0, 4.0]


def test_constants():
    assert VECTOR_MAX_DIMS == 2000
    assert HALFVEC_MAX_DIMS == 4000


def _provider_stub(config):
    class _P:
        def __init__(self, c): self.config = c; self.schema = "public"
    return _P(config)


def test_pgvector_hnsw_index_ddl_uses_halfvec_opclass():
    from primer.vector.pgvector import PgVectorStore
    store = PgVectorStore(_provider_stub(PgVectorConfig(**_common(), use_halfvec=True)))
    ddl = store._render_index_ddl(
        table_name="embeddings_x", index_name="embeddings_x_hnsw",
        opclass="halfvec_cosine_ops",
    )
    assert "USING hnsw (vector halfvec_cosine_ops)" in ddl


def test_pgvectorscale_diskann_index_ddl_uses_halfvec_opclass():
    from primer.vector.pgvectorscale import PgVectorScaleStore
    cfg = PgVectorScaleConfig(**_common(), use_halfvec=True, enable_diskann=True)
    store = PgVectorScaleStore(_provider_stub(cfg))
    ddl = store._render_index_ddl(
        table_name="embeddings_x", index_name="embeddings_x_diskann",
        opclass="halfvec_cosine_ops",
    )
    assert "USING diskann (vector halfvec_cosine_ops)" in ddl
