"""``InternalCollectionsConfig``: the row that is the system collection's search toggle.

The subsystem that used to read it (ingest, CDC worker, per-entity search) was removed (D18); the row, its REST surface and the system
collection's search lifecycle are covered by ``tests/api/test_internal_collections.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.internal import InternalCollectionsConfig


def test_internal_collections_config_requires_search_provider_id():
    with pytest.raises(ValidationError):
        InternalCollectionsConfig(
            id="_internal_collections_config",
            embedding_provider_id="emb-1",
            embedding_model="m1",
            # no search_provider_id
        )


def test_internal_collections_config_with_search_provider_id_constructs():
    cfg = InternalCollectionsConfig(
        id="_internal_collections_config",
        embedding_provider_id="emb-1",
        embedding_model="m1",
        search_provider_id="ssp-1",
    )
    assert cfg.search_provider_id == "ssp-1"
