"""Reserved-id constants and factory specs for auto-bootstrap providers.

Defines one reserved id per built-in provider kind, and a matching
factory-spec dict describing the CREATE body shape that
:class:`matrix.bootstrap.runner.BootstrapRunner` uses to upsert the
row idempotently on first boot.

Design note
-----------
Factory specs are plain dicts that exactly mirror the Pydantic model
fields so callers can do ``Model(**spec)`` directly.  They are NOT
consulted at registry lookup time — a reserved id that has been
bootstrapped lives in storage like any other row.  The lookup-time
role of reserved ids is limited to API protection (Task 3):
POST → 409, DELETE → 403.

Reserved ids by kind
--------------------
* ``local``         — :class:`matrix.model.workspace.WorkspaceProvider`
* ``huggingface``   — :class:`matrix.model.provider.EmbeddingProvider`
* ``lance``         — :class:`matrix.model.provider.SemanticSearchProvider`
* ``huggingface-ce``— :class:`matrix.model.provider.CrossEncoderProvider`

LLM providers are intentionally excluded — they require API keys and
must be provisioned explicitly by the operator.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Reserved-id string constants
# ---------------------------------------------------------------------------

RESERVED_LOCAL_WORKSPACE_PROVIDER: str = "local"
RESERVED_HUGGINGFACE_EMBEDDER: str = "huggingface"
RESERVED_LANCE_SSP: str = "lance"
RESERVED_HUGGINGFACE_CROSS_ENCODER: str = "huggingface-ce"

# Convenience set of all reserved ids — for quick membership tests in
# router guards (Task 3) and BootstrapRunner (Task 4).
ALL_RESERVED_IDS: frozenset[str] = frozenset({
    RESERVED_LOCAL_WORKSPACE_PROVIDER,
    RESERVED_HUGGINGFACE_EMBEDDER,
    RESERVED_LANCE_SSP,
    RESERVED_HUGGINGFACE_CROSS_ENCODER,
})

# ---------------------------------------------------------------------------
# Factory specs
#
# Each value is a dict whose keys match the Pydantic model's fields.
# ``BootstrapRunner`` passes these dicts to ``Model(**spec)`` after
# substituting any path templates (``~`` expansion, tmp_path injection).
#
# The ``~`` in path strings is intentionally left unresolved here so
# BootstrapRunner can expand them relative to its ``root_dir`` argument
# rather than the process cwd at import time.
# ---------------------------------------------------------------------------

# ---- local workspace provider -------------------------------------------

RESERVED_WORKSPACE_PROVIDERS: dict[str, dict] = {
    RESERVED_LOCAL_WORKSPACE_PROVIDER: {
        "id": RESERVED_LOCAL_WORKSPACE_PROVIDER,
        "provider": "local",
        "config": {
            "kind": "local",
            # Tilde is resolved by BootstrapRunner at creation time.
            "path": "~/.primer/workspaces",
        },
    },
}

# ---- HuggingFace embedder -----------------------------------------------
#
# BAAI/bge-small-en-v1.5 is a public model; no HF token is required.
# HuggingFaceConfig.token is a mandatory SecretStr, so we supply an
# empty string — the adapter converts empty strings to None when
# calling SentenceTransformer(..., token=None).

RESERVED_EMBEDDERS: dict[str, dict] = {
    RESERVED_HUGGINGFACE_EMBEDDER: {
        "id": RESERVED_HUGGINGFACE_EMBEDDER,
        "provider": "huggingface",
        "models": [
            {"name": "BAAI/bge-small-en-v1.5"},
        ],
        "config": {
            # Empty string → token=None inside HuggingFaceEmbedder._get_model.
            # bge-small-en-v1.5 is public; no real token needed.
            "token": "",
        },
        "limits": {
            "max_concurrency": 2,
        },
    },
}

# ---- LanceDB semantic-search provider -----------------------------------
#
# ``path`` uses a tilde prefix — BootstrapRunner expands it via
# Path("~/.primer/vector").expanduser() before constructing the model.

RESERVED_SSPS: dict[str, dict] = {
    RESERVED_LANCE_SSP: {
        "id": RESERVED_LANCE_SSP,
        "provider": "lance",
        "config": {
            # Tilde is resolved by BootstrapRunner at creation time.
            "path": "~/.primer/vector",
            "hnsw_m": 16,
            "hnsw_ef_construction": 64,
            "hnsw_ef_search": 40,
            "index_min_rows": 1000,
        },
    },
}

# ---- HuggingFace cross-encoder ------------------------------------------
#
# cross-encoder/ms-marco-MiniLM-L-6-v2 is public; no token required.
# HuggingFaceCrossEncoderConfig.token is optional (None is fine).

RESERVED_CROSS_ENCODERS: dict[str, dict] = {
    RESERVED_HUGGINGFACE_CROSS_ENCODER: {
        "id": RESERVED_HUGGINGFACE_CROSS_ENCODER,
        "provider": "huggingface",
        "models": [
            {
                "name": "cross-encoder/ms-marco-MiniLM-L-6-v2",
                "max_pair_length": None,
            },
        ],
        "config": {
            "token": None,
        },
        "limits": {
            "max_concurrency": 2,
        },
    },
}


__all__ = [
    "ALL_RESERVED_IDS",
    "RESERVED_CROSS_ENCODERS",
    "RESERVED_EMBEDDERS",
    "RESERVED_HUGGINGFACE_CROSS_ENCODER",
    "RESERVED_HUGGINGFACE_EMBEDDER",
    "RESERVED_LANCE_SSP",
    "RESERVED_LOCAL_WORKSPACE_PROVIDER",
    "RESERVED_SSPS",
    "RESERVED_WORKSPACE_PROVIDERS",
]
