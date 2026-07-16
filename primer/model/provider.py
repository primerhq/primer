"""Pydantic models describing LLM, embedding, and toolset provider configuration.

These types define how providers are declared in configuration: which
backend they talk to, which models are permitted, the provider-specific
connection details, and the rate limits the application should enforce
against them.

Three top-level provider kinds are supported:

* :class:`LLMProvider` — chat / completion backends.
* :class:`EmbeddingProvider` — vector-embedding backends.
* :class:`Toolset` — tool sources (internal registry or MCP server).

This module is a thin re-export facade. The provider models now live in
focused per-family submodules under :mod:`primer.model.providers`; they
are re-exported here so that the long-standing public interface
``from primer.model.provider import X`` keeps working unchanged for every
provider family (storage, vector / semantic-search, llm, embedding,
cross-encoder, toolset / MCP, secret, artifact-storage).
"""

from __future__ import annotations

# Re-exported third-party / stdlib names. Historically these were
# imported at the top of this module and were therefore importable as
# ``from primer.model.provider import Field`` etc. They are kept here so
# the public symbol surface of this module stays byte-identical.
from enum import Enum  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import ClassVar, Literal  # noqa: F401

from pydantic import (  # noqa: F401
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PositiveInt,
    SecretStr,
    model_validator,
)

from primer.model.common import Identifiable  # noqa: F401

# Per-family provider models. Each name below must remain importable as
# ``from primer.model.provider import <Name>`` -- this facade is the
# single point that guarantees that, so no call site needs to change.
# The private ``_``-prefixed bases are re-exported too so they stay
# importable from here exactly as before the split.
from primer.model.providers._shared import (  # noqa: F401
    Limits,
    _HttpApiKeyConfig,
)
from primer.model.providers.artifact import (  # noqa: F401
    ArtifactStorageProvider,
    ArtifactStorageProviderType,
    DbArtifactConfig,
    FilesystemArtifactConfig,
    S3ArtifactConfig,
)
from primer.model.providers.cross_encoder import (  # noqa: F401
    CrossEncoderModel,
    CrossEncoderProvider,
    CrossEncoderProviderType,
    HuggingFaceCrossEncoderConfig,
)
from primer.model.providers.embedding import (  # noqa: F401
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    HuggingFaceConfig,
    OpenAIConfig,
    OpenAIEmbeddingFlavor,
)
from primer.model.providers.llm import (  # noqa: F401
    AggregatedLLMConfig,
    AggregatedMember,
    AnthropicConfig,
    FailoverClasses,
    FailoverPoint,
    GoogleConfig,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OllamaConfig,
    OpenChatConfig,
    OpenChatFlavor,
    OpenResponsesConfig,
    OpenResponsesFlavor,
    OpenRouterConfig,
    RoutingStrategy,
)
from primer.model.providers.secret import (  # noqa: F401
    EnvSecretConfig,
    SecretProviderConfig,
    SecretProviderType,
)
from primer.model.providers.storage import (  # noqa: F401
    PoolConfig,
    PostgresConfig,
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
    _PostgresBaseConfig,
)
from primer.model.providers.toolset import (  # noqa: F401
    HttpConfig,
    McpConfig,
    OAuthClientCredentials,
    OAuthConfig,
    StdioConfig,
    Toolset,
    ToolsetProviderType,
    TransportType,
)
from primer.model.providers.vector import (  # noqa: F401
    LanceConfig,
    PgVectorConfig,
    PgVectorScaleConfig,
    SemanticSearchProvider,
    SemanticSearchProviderType,
    VectorStoreProviderConfig,
    VectorStoreProviderType,
    _DistanceMetric,
    _PgVectorBaseConfig,
)

__all__ = [
    "AggregatedLLMConfig",
    "AggregatedMember",
    "AnthropicConfig",
    "ArtifactStorageProvider",
    "ArtifactStorageProviderType",
    "CrossEncoderModel",
    "CrossEncoderProvider",
    "CrossEncoderProviderType",
    "DbArtifactConfig",
    "EmbeddingModel",
    "EmbeddingProvider",
    "EmbeddingProviderType",
    "EnvSecretConfig",
    "FailoverClasses",
    "FailoverPoint",
    "FilesystemArtifactConfig",
    "GoogleConfig",
    "HttpConfig",
    "HuggingFaceConfig",
    "HuggingFaceCrossEncoderConfig",
    "LLMModel",
    "LLMProvider",
    "LLMProviderType",
    "LanceConfig",
    "Limits",
    "McpConfig",
    "OAuthClientCredentials",
    "OAuthConfig",
    "OllamaConfig",
    "OpenAIConfig",
    "OpenAIEmbeddingFlavor",
    "OpenChatConfig",
    "OpenChatFlavor",
    "OpenResponsesConfig",
    "OpenResponsesFlavor",
    "OpenRouterConfig",
    "PgVectorConfig",
    "PgVectorScaleConfig",
    "PoolConfig",
    "PostgresConfig",
    "RoutingStrategy",
    "S3ArtifactConfig",
    "SecretProviderConfig",
    "SecretProviderType",
    "SemanticSearchProvider",
    "SemanticSearchProviderType",
    "SqliteConfig",
    "StdioConfig",
    "StorageProviderConfig",
    "StorageProviderType",
    "Toolset",
    "ToolsetProviderType",
    "TransportType",
    "VectorStoreProviderConfig",
    "VectorStoreProviderType",
]
