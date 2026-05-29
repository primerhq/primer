"""Tests for reserved-id constants and factory specs in defaults.py.

Verifies that:
- The four reserved-id string constants have the expected values.
- Each factory-spec dict is present in its respective registry dict.
- Each factory spec can be used to construct the corresponding Pydantic
  model (structural validity).
- The registry sets in provider_registry.py contain the expected ids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.bootstrap.defaults import (
    ALL_RESERVED_IDS,
    RESERVED_CROSS_ENCODERS,
    RESERVED_EMBEDDERS,
    RESERVED_HUGGINGFACE_CROSS_ENCODER,
    RESERVED_HUGGINGFACE_EMBEDDER,
    RESERVED_LANCE_SSP,
    RESERVED_LOCAL_WORKSPACE_PROVIDER,
    RESERVED_LOCAL_WORKSPACE_TEMPLATE,
    RESERVED_SSPS,
    RESERVED_WORKSPACE_PROVIDERS,
    RESERVED_WORKSPACE_TEMPLATES,
)
from primer.api.registries.provider_registry import (
    RESERVED_CROSS_ENCODER_IDS,
    RESERVED_EMBEDDER_IDS,
    RESERVED_LLM_IDS,
    RESERVED_SSP_IDS,
    RESERVED_TOOLSET_IDS,
    RESERVED_WORKSPACE_PROVIDER_IDS,
)


# ---------------------------------------------------------------------------
# String constant values
# ---------------------------------------------------------------------------


def test_reserved_ids_constants():
    assert RESERVED_LOCAL_WORKSPACE_PROVIDER == "local"
    assert RESERVED_HUGGINGFACE_EMBEDDER == "huggingface"
    assert RESERVED_LANCE_SSP == "lance"
    assert RESERVED_HUGGINGFACE_CROSS_ENCODER == "huggingface-ce"
    assert RESERVED_LOCAL_WORKSPACE_TEMPLATE == "local-default"


def test_all_reserved_ids_contains_all_four():
    assert RESERVED_LOCAL_WORKSPACE_PROVIDER in ALL_RESERVED_IDS
    assert RESERVED_HUGGINGFACE_EMBEDDER in ALL_RESERVED_IDS
    assert RESERVED_LANCE_SSP in ALL_RESERVED_IDS
    assert RESERVED_HUGGINGFACE_CROSS_ENCODER in ALL_RESERVED_IDS
    assert RESERVED_LOCAL_WORKSPACE_TEMPLATE in ALL_RESERVED_IDS
    assert len(ALL_RESERVED_IDS) == 5


# ---------------------------------------------------------------------------
# Factory spec presence
# ---------------------------------------------------------------------------


def test_factory_specs_present():
    assert RESERVED_HUGGINGFACE_EMBEDDER in RESERVED_EMBEDDERS
    assert RESERVED_LANCE_SSP in RESERVED_SSPS
    assert RESERVED_HUGGINGFACE_CROSS_ENCODER in RESERVED_CROSS_ENCODERS
    assert RESERVED_LOCAL_WORKSPACE_PROVIDER in RESERVED_WORKSPACE_PROVIDERS
    assert RESERVED_LOCAL_WORKSPACE_TEMPLATE in RESERVED_WORKSPACE_TEMPLATES


# ---------------------------------------------------------------------------
# Factory spec structural validity (Pydantic model construction)
# ---------------------------------------------------------------------------


def test_embedder_spec_constructs_pydantic_model():
    """HuggingFace embedder spec must produce a valid EmbeddingProvider."""
    from primer.model.provider import EmbeddingProvider

    spec = RESERVED_EMBEDDERS[RESERVED_HUGGINGFACE_EMBEDDER].copy()
    provider = EmbeddingProvider(**spec)
    assert provider.id == RESERVED_HUGGINGFACE_EMBEDDER
    assert provider.provider.value == "huggingface"
    assert len(provider.models) == 1
    assert provider.models[0].name == "BAAI/bge-small-en-v1.5"
    assert provider.limits.max_concurrency == 2


def test_cross_encoder_spec_constructs_pydantic_model():
    """HuggingFace cross-encoder spec must produce a valid CrossEncoderProvider."""
    from primer.model.provider import CrossEncoderProvider

    spec = RESERVED_CROSS_ENCODERS[RESERVED_HUGGINGFACE_CROSS_ENCODER].copy()
    provider = CrossEncoderProvider(**spec)
    assert provider.id == RESERVED_HUGGINGFACE_CROSS_ENCODER
    assert provider.provider.value == "huggingface"
    assert len(provider.models) == 1
    assert provider.models[0].name == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert provider.limits.max_concurrency == 2


def test_ssp_spec_constructs_pydantic_model(tmp_path):
    """LanceDB SSP spec (with path resolved) must produce a valid SemanticSearchProvider."""
    from primer.model.provider import LanceConfig, SemanticSearchProvider

    raw_spec = RESERVED_SSPS[RESERVED_LANCE_SSP]
    spec = raw_spec.copy()
    # Simulate BootstrapRunner path resolution.
    config_dict = dict(raw_spec["config"])
    config_dict["path"] = tmp_path / "vector"
    spec = {**raw_spec, "config": config_dict}

    provider = SemanticSearchProvider(**spec)
    assert provider.id == RESERVED_LANCE_SSP
    assert provider.provider.value == "lance"
    assert isinstance(provider.config, LanceConfig)


def test_workspace_provider_spec_constructs_pydantic_model(tmp_path):
    """Local workspace provider spec (with path resolved) must produce a valid WorkspaceProvider."""
    from primer.model.workspace import WorkspaceProvider

    raw_spec = RESERVED_WORKSPACE_PROVIDERS[RESERVED_LOCAL_WORKSPACE_PROVIDER]
    config_dict = dict(raw_spec["config"])
    config_dict["root_path"] = str(tmp_path / "workspaces")
    spec = {**raw_spec, "config": config_dict}

    provider = WorkspaceProvider(**spec)
    assert provider.id == RESERVED_LOCAL_WORKSPACE_PROVIDER
    assert provider.provider.value == "local"


# ---------------------------------------------------------------------------
# Registry frozenset membership (provider_registry.py)
# ---------------------------------------------------------------------------


def test_registry_embedder_ids_contains_reserved():
    assert RESERVED_HUGGINGFACE_EMBEDDER in RESERVED_EMBEDDER_IDS


def test_registry_ssp_ids_contains_reserved():
    assert RESERVED_LANCE_SSP in RESERVED_SSP_IDS


def test_registry_cross_encoder_ids_contains_reserved():
    assert RESERVED_HUGGINGFACE_CROSS_ENCODER in RESERVED_CROSS_ENCODER_IDS


def test_registry_workspace_provider_ids_contains_reserved():
    assert RESERVED_LOCAL_WORKSPACE_PROVIDER in RESERVED_WORKSPACE_PROVIDER_IDS


def test_registry_llm_ids_is_empty():
    """LLMs have no reserved ids — operators must provision them explicitly."""
    assert len(RESERVED_LLM_IDS) == 0


def test_registry_toolset_ids_unchanged():
    """Toolset reserved ids still include the original built-in set."""
    for tid in ("system", "search", "workspaces", "misc", "web", "harness"):
        assert tid in RESERVED_TOOLSET_IDS


# ---------------------------------------------------------------------------
# Spec id field must match the dict key
# ---------------------------------------------------------------------------


def test_spec_ids_match_keys():
    for key, spec in RESERVED_EMBEDDERS.items():
        assert spec["id"] == key, f"RESERVED_EMBEDDERS key {key!r} != spec id {spec['id']!r}"
    for key, spec in RESERVED_SSPS.items():
        assert spec["id"] == key, f"RESERVED_SSPS key {key!r} != spec id {spec['id']!r}"
    for key, spec in RESERVED_CROSS_ENCODERS.items():
        assert spec["id"] == key, f"RESERVED_CROSS_ENCODERS key {key!r} != spec id {spec['id']!r}"
    for key, spec in RESERVED_WORKSPACE_PROVIDERS.items():
        assert spec["id"] == key, f"RESERVED_WORKSPACE_PROVIDERS key {key!r} != spec id {spec['id']!r}"
    for key, spec in RESERVED_WORKSPACE_TEMPLATES.items():
        assert spec["id"] == key, f"RESERVED_WORKSPACE_TEMPLATES key {key!r} != spec id {spec['id']!r}"
