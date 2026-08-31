"""v2 Document: tree fields (parent_id, slug), timestamps, no name."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.collection import Document


def test_tree_fields_and_defaults() -> None:
    d = Document(collection_id="c1", slug="intro", path="guides/intro")
    assert d.parent_id is None
    assert d.title is None
    assert d.created_at is not None and d.updated_at is not None
    assert d.id.startswith("document-")
    assert "name" not in Document.model_fields


def test_slug_charset_rejects_bad_chars() -> None:
    with pytest.raises(ValidationError):
        Document(collection_id="c1", slug="Has Space", path="has-space")
    with pytest.raises(ValidationError):
        Document(collection_id="c1", slug="a/b", path="a-b")


def test_transitional_slug_accepts_dotted_leaf() -> None:
    d = Document(collection_id="c1", slug="slo.md", path="concepts/slo.md")
    assert d.slug == "slo.md"


def test_path_validator_survives() -> None:
    with pytest.raises(ValidationError):
        Document(collection_id="c1", slug="a", path="/leading")
