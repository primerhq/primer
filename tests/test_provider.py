"""Unit tests for the new Toolset / McpConfig types in matrix/model/provider.py.

Covers:

* :class:`StdioConfig` and :class:`HttpConfig` — transport-specific configs.
* :class:`McpConfig` — wraps transport + config with a consistency
  validator.
* :class:`Toolset` — composes from :class:`Identifiable`; provider +
  optional config with a consistency validator.
* :class:`ToolsetProviderType` and :class:`TransportType` enums.
"""

from __future__ import annotations

import pytest
from pydantic import HttpUrl, SecretStr, ValidationError

from matrix.model.common import Identifiable
from matrix.model.provider import (
    GoogleConfig,
    HttpConfig,
    McpConfig,
    OpenAIConfig,
    OpenAIEmbeddingFlavor,
    PgVectorConfig,
    PgVectorScaleConfig,
    PoolConfig,
    SemanticSearchProvider,
    SemanticSearchProviderType,
    StdioConfig,
    Toolset,
    ToolsetProviderType,
    TransportType,
)


# ============================================================================
# StdioConfig
# ============================================================================


class TestStdioConfig:
    def test_construction_minimal(self):
        cfg = StdioConfig(command=["mcp-server"])
        assert cfg.command == ["mcp-server"]
        assert cfg.env == {}  # default

    def test_construction_with_env(self):
        cfg = StdioConfig(
            command=["mcp-server", "--root", "/data"],
            env={"LOG_LEVEL": "info", "TZ": "UTC"},
        )
        assert cfg.command == ["mcp-server", "--root", "/data"]
        assert cfg.env == {"LOG_LEVEL": "info", "TZ": "UTC"}

    def test_command_required(self):
        with pytest.raises(ValidationError):
            StdioConfig()

    def test_command_must_be_non_empty(self):
        # min_length=1 on the command list.
        with pytest.raises(ValidationError):
            StdioConfig(command=[])

    def test_command_must_be_list_of_strings(self):
        with pytest.raises(ValidationError):
            StdioConfig(command="not a list")

    def test_env_defaults_to_empty_dict_not_shared(self):
        # Defensive — default_factory should produce independent dicts.
        cfg1 = StdioConfig(command=["x"])
        cfg2 = StdioConfig(command=["y"])
        cfg1.env["TEST"] = "1"
        assert "TEST" not in cfg2.env

    def test_json_round_trip(self):
        cfg = StdioConfig(command=["a", "b"], env={"KEY": "value"})
        restored = StdioConfig.model_validate_json(cfg.model_dump_json())
        assert restored.command == cfg.command
        assert restored.env == cfg.env


# ============================================================================
# HttpConfig
# ============================================================================


class TestHttpConfig:
    def test_construction_minimal(self):
        cfg = HttpConfig(url="https://mcp.example.com")
        assert cfg.url == "https://mcp.example.com"
        assert cfg.headers == {}  # default

    def test_construction_with_headers(self):
        cfg = HttpConfig(
            url="https://mcp.example.com/v1",
            headers={"Authorization": "Bearer test-token", "X-Custom": "v"},
        )
        assert cfg.url == "https://mcp.example.com/v1"
        assert cfg.headers == {"Authorization": "Bearer test-token", "X-Custom": "v"}

    def test_url_required(self):
        with pytest.raises(ValidationError):
            HttpConfig()

    def test_url_min_length(self):
        # min_length=1 on the URL.
        with pytest.raises(ValidationError):
            HttpConfig(url="")

    def test_headers_defaults_to_empty_dict_not_shared(self):
        cfg1 = HttpConfig(url="https://a")
        cfg2 = HttpConfig(url="https://b")
        cfg1.headers["X"] = "1"
        assert "X" not in cfg2.headers

    def test_json_round_trip(self):
        cfg = HttpConfig(url="https://x", headers={"Authorization": "Bearer y"})
        restored = HttpConfig.model_validate_json(cfg.model_dump_json())
        assert restored.url == cfg.url
        assert restored.headers == cfg.headers


# ============================================================================
# TransportType enum
# ============================================================================


class TestTransportType:
    def test_stdio_value(self):
        assert TransportType.STDIO.value == "stdio"

    def test_http_value(self):
        assert TransportType.HTTP.value == "http"

    def test_only_two_members(self):
        assert {t.value for t in TransportType} == {"stdio", "http"}

    def test_string_inheritance(self):
        # Should be a str subclass for JSON-friendliness.
        assert isinstance(TransportType.STDIO, str)


# ============================================================================
# McpConfig — transport + config with consistency validator
# ============================================================================


class TestMcpConfig:
    def test_stdio_construction(self):
        cfg = McpConfig(
            transport=TransportType.STDIO,
            config=StdioConfig(command=["mcp-server-fs"]),
        )
        assert cfg.transport == TransportType.STDIO
        assert isinstance(cfg.config, StdioConfig)

    def test_http_construction(self):
        cfg = McpConfig(
            transport=TransportType.HTTP,
            config=HttpConfig(url="https://mcp.example.com"),
        )
        assert cfg.transport == TransportType.HTTP
        assert isinstance(cfg.config, HttpConfig)

    def test_stdio_with_string_enum_value(self):
        # Pydantic accepts the str value of the enum.
        cfg = McpConfig(
            transport="stdio",
            config=StdioConfig(command=["x"]),
        )
        assert cfg.transport == TransportType.STDIO

    def test_http_with_string_enum_value(self):
        cfg = McpConfig(
            transport="http",
            config=HttpConfig(url="https://x"),
        )
        assert cfg.transport == TransportType.HTTP

    def test_stdio_transport_with_http_config_rejected(self):
        with pytest.raises(ValidationError, match="stdio.*StdioConfig"):
            McpConfig(
                transport=TransportType.STDIO,
                config=HttpConfig(url="https://x"),
            )

    def test_http_transport_with_stdio_config_rejected(self):
        with pytest.raises(ValidationError, match="http.*HttpConfig"):
            McpConfig(
                transport=TransportType.HTTP,
                config=StdioConfig(command=["x"]),
            )

    def test_unknown_transport_rejected(self):
        with pytest.raises(ValidationError):
            McpConfig(
                transport="websocket",
                config=StdioConfig(command=["x"]),
            )

    def test_transport_required(self):
        with pytest.raises(ValidationError):
            McpConfig(config=StdioConfig(command=["x"]))

    def test_config_required(self):
        with pytest.raises(ValidationError):
            McpConfig(transport=TransportType.STDIO)

    def test_dict_construction_stdio(self):
        # Round-trip from a plain dict (e.g. config-file deserialisation).
        cfg = McpConfig.model_validate(
            {
                "transport": "stdio",
                "config": {"command": ["mcp-server"], "env": {"LOG": "info"}},
            }
        )
        assert isinstance(cfg.config, StdioConfig)
        assert cfg.config.command == ["mcp-server"]

    def test_dict_construction_http(self):
        cfg = McpConfig.model_validate(
            {
                "transport": "http",
                "config": {
                    "url": "https://mcp.example.com",
                    "headers": {"Authorization": "Bearer x"},
                },
            }
        )
        assert isinstance(cfg.config, HttpConfig)
        assert cfg.config.url == "https://mcp.example.com"

    def test_dict_construction_mismatch_rejected(self):
        # The validator catches mismatches even at dict-validation time.
        with pytest.raises(ValidationError):
            McpConfig.model_validate(
                {
                    "transport": "stdio",
                    "config": {"url": "https://x"},  # HttpConfig shape
                }
            )

    def test_json_round_trip_stdio(self):
        cfg = McpConfig(
            transport=TransportType.STDIO,
            config=StdioConfig(command=["a", "b"], env={"K": "v"}),
        )
        restored = McpConfig.model_validate_json(cfg.model_dump_json())
        assert restored.transport == TransportType.STDIO
        assert isinstance(restored.config, StdioConfig)
        assert restored.config.command == ["a", "b"]
        assert restored.config.env == {"K": "v"}

    def test_json_round_trip_http(self):
        cfg = McpConfig(
            transport=TransportType.HTTP,
            config=HttpConfig(url="https://x", headers={"H": "v"}),
        )
        restored = McpConfig.model_validate_json(cfg.model_dump_json())
        assert restored.transport == TransportType.HTTP
        assert isinstance(restored.config, HttpConfig)
        assert restored.config.url == "https://x"


# ============================================================================
# ToolsetProviderType enum
# ============================================================================


class TestToolsetProviderType:
    def test_internal_value(self):
        assert ToolsetProviderType.INTERNAL.value == "internal"

    def test_mcp_value(self):
        assert ToolsetProviderType.MCP.value == "mcp"

    def test_only_two_members(self):
        assert {t.value for t in ToolsetProviderType} == {"internal", "mcp"}

    def test_string_inheritance(self):
        assert isinstance(ToolsetProviderType.INTERNAL, str)


# ============================================================================
# Toolset — composes from Identifiable, follows the provider pattern
# ============================================================================


class TestToolsetInternal:
    def test_internal_construction_minimal(self):
        ts = Toolset(id="builtins", provider=ToolsetProviderType.INTERNAL)
        assert ts.id == "builtins"
        assert ts.provider == ToolsetProviderType.INTERNAL
        assert ts.config is None

    def test_internal_with_string_provider(self):
        ts = Toolset(id="builtins", provider="internal")
        assert ts.provider == ToolsetProviderType.INTERNAL

    def test_internal_with_explicit_none_config(self):
        ts = Toolset(id="x", provider="internal", config=None)
        assert ts.config is None

    def test_internal_with_config_rejected(self):
        with pytest.raises(ValidationError, match="internal.*must not have"):
            Toolset(
                id="x",
                provider=ToolsetProviderType.INTERNAL,
                config=McpConfig(
                    transport=TransportType.STDIO,
                    config=StdioConfig(command=["x"]),
                ),
            )

    def test_internal_inherits_from_identifiable(self):
        ts = Toolset(id="x", provider="internal")
        assert isinstance(ts, Identifiable)


class TestToolsetMcp:
    def test_mcp_stdio_construction(self):
        ts = Toolset(
            id="filesystem",
            provider=ToolsetProviderType.MCP,
            config=McpConfig(
                transport=TransportType.STDIO,
                config=StdioConfig(command=["mcp-server-fs", "--root", "/data"]),
            ),
        )
        assert ts.provider == ToolsetProviderType.MCP
        assert isinstance(ts.config, McpConfig)
        assert ts.config.transport == TransportType.STDIO

    def test_mcp_http_construction(self):
        ts = Toolset(
            id="remote",
            provider=ToolsetProviderType.MCP,
            config=McpConfig(
                transport=TransportType.HTTP,
                config=HttpConfig(
                    url="https://mcp.example.com",
                    headers={"Authorization": "Bearer test-token"},
                ),
            ),
        )
        assert ts.provider == ToolsetProviderType.MCP
        assert isinstance(ts.config.config, HttpConfig)

    def test_mcp_with_string_provider(self):
        ts = Toolset(
            id="x",
            provider="mcp",
            config=McpConfig(
                transport="stdio",
                config=StdioConfig(command=["x"]),
            ),
        )
        assert ts.provider == ToolsetProviderType.MCP

    def test_mcp_without_config_rejected(self):
        with pytest.raises(ValidationError, match="mcp.*requires.*McpConfig"):
            Toolset(id="x", provider=ToolsetProviderType.MCP)

    def test_mcp_with_explicit_none_config_rejected(self):
        with pytest.raises(ValidationError):
            Toolset(id="x", provider="mcp", config=None)


class TestToolsetCommon:
    def test_id_required(self):
        with pytest.raises(ValidationError):
            Toolset(provider="internal")

    def test_id_min_length(self):
        # Identifiable enforces min_length=1 on id.
        with pytest.raises(ValidationError):
            Toolset(id="", provider="internal")

    def test_provider_required(self):
        with pytest.raises(ValidationError):
            Toolset(id="x")

    def test_unknown_provider_rejected(self):
        with pytest.raises(ValidationError):
            Toolset(id="x", provider="bogus")

    def test_dict_construction_internal(self):
        ts = Toolset.model_validate({"id": "builtins", "provider": "internal"})
        assert ts.provider == ToolsetProviderType.INTERNAL

    def test_dict_construction_mcp_stdio(self):
        ts = Toolset.model_validate(
            {
                "id": "fs",
                "provider": "mcp",
                "config": {
                    "transport": "stdio",
                    "config": {"command": ["mcp-server-fs"]},
                },
            }
        )
        assert ts.provider == ToolsetProviderType.MCP
        assert isinstance(ts.config.config, StdioConfig)

    def test_dict_construction_mcp_http(self):
        ts = Toolset.model_validate(
            {
                "id": "remote",
                "provider": "mcp",
                "config": {
                    "transport": "http",
                    "config": {"url": "https://mcp.example.com"},
                },
            }
        )
        assert ts.provider == ToolsetProviderType.MCP
        assert isinstance(ts.config.config, HttpConfig)

    def test_json_round_trip_internal(self):
        ts = Toolset(id="builtins", provider="internal")
        restored = Toolset.model_validate_json(ts.model_dump_json())
        assert restored.id == "builtins"
        assert restored.provider == ToolsetProviderType.INTERNAL
        assert restored.config is None

    def test_json_round_trip_mcp_stdio(self):
        ts = Toolset(
            id="fs",
            provider="mcp",
            config=McpConfig(
                transport="stdio",
                config=StdioConfig(command=["mcp-server-fs"], env={"LOG": "debug"}),
            ),
        )
        restored = Toolset.model_validate_json(ts.model_dump_json())
        assert restored.id == "fs"
        assert restored.config.transport == TransportType.STDIO
        assert restored.config.config.command == ["mcp-server-fs"]
        assert restored.config.config.env == {"LOG": "debug"}

    def test_json_round_trip_mcp_http(self):
        ts = Toolset(
            id="remote",
            provider="mcp",
            config=McpConfig(
                transport="http",
                config=HttpConfig(
                    url="https://mcp.example.com",
                    headers={"Authorization": "Bearer x"},
                ),
            ),
        )
        restored = Toolset.model_validate_json(ts.model_dump_json())
        assert restored.config.transport == TransportType.HTTP
        assert restored.config.config.url == "https://mcp.example.com"
        assert restored.config.config.headers == {"Authorization": "Bearer x"}


# ============================================================================
# OpenAIEmbeddingFlavor — flavor discriminator for OpenAIConfig
# ============================================================================


class TestOpenAIEmbeddingFlavor:
    def test_default_flavor_is_other(self) -> None:
        cfg = OpenAIConfig(
            url=HttpUrl("https://api.openai.com/v1/"),
            api_key=SecretStr("sk-test"),
        )
        assert cfg.flavor is OpenAIEmbeddingFlavor.OTHER

    def test_explicit_openai_flavor(self) -> None:
        cfg = OpenAIConfig(
            url=HttpUrl("https://api.openai.com/v1/"),
            api_key=SecretStr("sk-test"),
            flavor=OpenAIEmbeddingFlavor.OPENAI,
        )
        assert cfg.flavor is OpenAIEmbeddingFlavor.OPENAI

    def test_explicit_lmstudio_flavor(self) -> None:
        cfg = OpenAIConfig(
            url=HttpUrl("http://localhost:1234/v1/"),
            api_key=SecretStr(""),
            flavor=OpenAIEmbeddingFlavor.LMSTUDIO,
        )
        assert cfg.flavor is OpenAIEmbeddingFlavor.LMSTUDIO

    def test_flavor_serializes_as_string(self) -> None:
        assert OpenAIEmbeddingFlavor.OPENAI.value == "openai"
        assert OpenAIEmbeddingFlavor.LMSTUDIO.value == "lmstudio"
        assert OpenAIEmbeddingFlavor.OTHER.value == "other"


class TestGeminiProviderType:
    def test_gemini_enum_value(self) -> None:
        from matrix.model.provider import LLMProviderType

        assert LLMProviderType.GEMINI.value == "gemini"

    def test_existing_openresponses_value_unchanged(self) -> None:
        from matrix.model.provider import LLMProviderType

        assert LLMProviderType.OPENRESPONSES.value == "openresponses"


class TestGoogleConfig:
    def test_constructed_with_api_key(self) -> None:
        from pydantic import SecretStr

        cfg = GoogleConfig(api_key=SecretStr("api-key-test"))
        assert cfg.api_key.get_secret_value() == "api-key-test"

    def test_accepts_missing_api_key(self) -> None:
        """api_key is optional so operators can register endpoints
        fronted by an auth-injecting proxy. The real Gemini API will
        surface 401 at call time if a key is actually required."""
        cfg = GoogleConfig()
        assert cfg.api_key is None

    def test_no_url_field(self) -> None:
        # Gemini API endpoint is fixed by the SDK; no URL field.
        from pydantic import SecretStr

        cfg = GoogleConfig(api_key=SecretStr("k"))
        assert "url" not in cfg.model_dump()


class TestLLMProviderConfigUnion:
    def test_accepts_openresponses_config(self) -> None:
        from pydantic import HttpUrl, SecretStr
        from matrix.model.provider import (
            LLMModel,
            LLMProvider,
            LLMProviderType,
            Limits,
            OpenResponsesConfig,
        )

        provider = LLMProvider(
            id="o1",
            provider=LLMProviderType.OPENRESPONSES,
            models=[LLMModel(name="gpt-4o-mini", context_length=8192)],
            config=OpenResponsesConfig(
                url=HttpUrl("https://api.openai.com/v1/"),
                api_key=SecretStr("sk-x"),
            ),
            limits=Limits(max_concurrency=2),
        )
        assert isinstance(provider.config, OpenResponsesConfig)

    def test_accepts_google_config(self) -> None:
        from pydantic import SecretStr
        from matrix.model.provider import (
            LLMModel,
            LLMProvider,
            LLMProviderType,
            Limits,
        )

        provider = LLMProvider(
            id="g1",
            provider=LLMProviderType.GEMINI,
            models=[LLMModel(name="gemini-2.5-flash", context_length=1_000_000)],
            config=GoogleConfig(api_key=SecretStr("api-x")),
            limits=Limits(max_concurrency=1),
        )
        assert isinstance(provider.config, GoogleConfig)
        assert provider.config.api_key.get_secret_value() == "api-x"


from matrix.model.provider import AnthropicConfig


class TestAnthropicProviderType:
    def test_anthropic_enum_value(self) -> None:
        from matrix.model.provider import LLMProviderType
        assert LLMProviderType.ANTHROPIC.value == "anthropic"

    def test_existing_values_unchanged(self) -> None:
        from matrix.model.provider import LLMProviderType
        assert LLMProviderType.OPENRESPONSES.value == "openresponses"
        assert LLMProviderType.GEMINI.value == "gemini"


class TestAnthropicConfig:
    def test_constructed_with_api_key(self) -> None:
        from pydantic import SecretStr
        cfg = AnthropicConfig(api_key=SecretStr("sk-ant-test"))
        assert cfg.api_key.get_secret_value() == "sk-ant-test"

    def test_accepts_missing_api_key(self) -> None:
        """api_key is optional so operators can register endpoints
        fronted by an auth-injecting proxy. The real Anthropic API
        will surface 401 at call time if a key is actually required."""
        cfg = AnthropicConfig()
        assert cfg.api_key is None


class TestAnthropicConfigInUnion:
    def test_llm_provider_accepts_anthropic_config(self) -> None:
        from pydantic import SecretStr
        from matrix.model.provider import (
            LLMModel, LLMProvider, LLMProviderType, Limits,
        )
        provider = LLMProvider(
            id="a1",
            provider=LLMProviderType.ANTHROPIC,
            models=[LLMModel(name="claude-sonnet-4-5", context_length=200_000)],
            config=AnthropicConfig(api_key=SecretStr("sk-ant-x")),
            limits=Limits(max_concurrency=2),
        )
        assert isinstance(provider.config, AnthropicConfig)


class TestGeminiEmbeddingProviderType:
    def test_gemini_enum_value(self) -> None:
        from matrix.model.provider import EmbeddingProviderType
        assert EmbeddingProviderType.GEMINI.value == "gemini"

    def test_existing_values_unchanged(self) -> None:
        from matrix.model.provider import EmbeddingProviderType
        assert EmbeddingProviderType.OPENAI.value == "openai"
        assert EmbeddingProviderType.HUGGINGFACE.value == "huggingface"


class TestEmbeddingProviderConfigUnionGoogle:
    def test_accepts_google_config_for_gemini(self) -> None:
        from pydantic import SecretStr
        from matrix.model.provider import (
            EmbeddingModel,
            EmbeddingProvider,
            EmbeddingProviderType,
            GoogleConfig,
            Limits,
        )
        provider = EmbeddingProvider(
            id="g1",
            provider=EmbeddingProviderType.GEMINI,
            models=[EmbeddingModel(name="text-embedding-004")],
            config=GoogleConfig(api_key=SecretStr("api-x")),
            limits=Limits(max_concurrency=2),
        )
        assert isinstance(provider.config, GoogleConfig)


class TestOllamaProviderType:
    def test_ollama_enum_value(self) -> None:
        from matrix.model.provider import LLMProviderType
        assert LLMProviderType.OLLAMA.value == "ollama"


class TestOllamaConfig:
    def test_constructed_with_url_only(self) -> None:
        from pydantic import HttpUrl
        from matrix.model.provider import OllamaConfig
        cfg = OllamaConfig(url=HttpUrl("http://localhost:11434"))
        assert cfg.api_key is None

    def test_constructed_with_url_and_api_key(self) -> None:
        from pydantic import HttpUrl, SecretStr
        from matrix.model.provider import OllamaConfig
        cfg = OllamaConfig(
            url=HttpUrl("https://ollama.example.com"),
            api_key=SecretStr("secret"),
        )
        assert cfg.api_key.get_secret_value() == "secret"

    def test_rejects_missing_url(self) -> None:
        from pydantic import ValidationError
        from matrix.model.provider import OllamaConfig
        with pytest.raises(ValidationError):
            OllamaConfig()  # type: ignore[call-arg]


class TestOllamaConfigInUnion:
    def test_llm_provider_accepts_ollama_config(self) -> None:
        from pydantic import HttpUrl
        from matrix.model.provider import (
            LLMModel, LLMProvider, LLMProviderType, Limits, OllamaConfig,
        )
        provider = LLMProvider(
            id="o1",
            provider=LLMProviderType.OLLAMA,
            models=[LLMModel(name="llama3", context_length=8192)],
            config=OllamaConfig(url=HttpUrl("http://localhost:11434")),
            limits=Limits(max_concurrency=2),
        )
        assert isinstance(provider.config, OllamaConfig)


class TestToolsetProviderReexports:
    """ToolsetProvider and its concrete implementations are reachable
    from their documented import paths."""

    def test_toolset_provider_abc_reachable_from_matrix_int(self) -> None:
        from matrix.int import ToolsetProvider as A
        from matrix.int.toolset import ToolsetProvider as B
        assert A is B

    def test_concrete_providers_reachable_from_matrix_toolset(self) -> None:
        from matrix.toolset import InternalToolsetProvider as I
        from matrix.toolset import McpToolsetProvider as M
        from matrix.toolset.internal import InternalToolsetProvider as I2
        from matrix.toolset.mcp import McpToolsetProvider as M2
        assert I is I2
        assert M is M2


# ============================================================================
# OAuth configuration models attached to HttpConfig
# ============================================================================


class TestOAuthClientCredentials:
    def test_minimal_public_client(self) -> None:
        from matrix.model.provider import OAuthClientCredentials

        c = OAuthClientCredentials(client_id="abc")
        assert c.client_id == "abc"
        assert c.client_secret is None

    def test_confidential_client(self) -> None:
        from matrix.model.provider import OAuthClientCredentials

        c = OAuthClientCredentials(
            client_id="abc",
            client_secret="shh",
        )
        assert c.client_secret is not None
        assert c.client_secret.get_secret_value() == "shh"

    def test_empty_client_id_rejected(self) -> None:
        import pytest
        from pydantic import ValidationError

        from matrix.model.provider import OAuthClientCredentials

        with pytest.raises(ValidationError):
            OAuthClientCredentials(client_id="")


class TestOAuthConfig:
    def test_minimal_oauth_config(self) -> None:
        from matrix.model.provider import OAuthConfig

        c = OAuthConfig(redirect_uri="https://app.example/callback")
        assert str(c.redirect_uri).startswith("https://app.example/callback")
        assert c.scopes == []
        assert c.resource_uri is None
        assert c.static_client is None
        assert c.spec_version is None
        assert c.client_name == "matrix"

    def test_with_scopes_and_static_client(self) -> None:
        from matrix.model.provider import OAuthClientCredentials, OAuthConfig

        c = OAuthConfig(
            redirect_uri="https://app.example/cb",
            scopes=["read", "write"],
            static_client=OAuthClientCredentials(
                client_id="abc",
                client_secret="shh",
            ),
            spec_version="2025-06-18",
        )
        assert c.scopes == ["read", "write"]
        assert c.static_client is not None
        assert c.static_client.client_id == "abc"
        assert c.spec_version == "2025-06-18"

    def test_invalid_spec_version_rejected(self) -> None:
        import pytest
        from pydantic import ValidationError

        from matrix.model.provider import OAuthConfig

        with pytest.raises(ValidationError):
            OAuthConfig(
                redirect_uri="https://app.example/cb",
                spec_version="2024-01-01",
            )


class TestHttpConfigOAuth:
    def test_default_oauth_is_none(self) -> None:
        from matrix.model.provider import HttpConfig

        c = HttpConfig(url="http://localhost:9999/mcp")
        assert c.oauth is None

    def test_oauth_can_be_attached(self) -> None:
        from matrix.model.provider import HttpConfig, OAuthConfig

        c = HttpConfig(
            url="http://localhost:9999/mcp",
            oauth=OAuthConfig(redirect_uri="https://app.example/cb"),
        )
        assert c.oauth is not None
        assert str(c.oauth.redirect_uri).startswith("https://app.example/cb")


def test_semantic_search_provider_pgvector_construction():
    ssp = SemanticSearchProvider(
        id="ssp-test",
        provider=SemanticSearchProviderType.PGVECTOR,
        config=PgVectorConfig(
            hostname="h",
            username="u",
            password=SecretStr("p"),
            database="db",
            pool=PoolConfig(),
        ),
    )
    assert ssp.id == "ssp-test"
    assert ssp.provider == SemanticSearchProviderType.PGVECTOR
    assert isinstance(ssp.config, PgVectorConfig)


def test_semantic_search_provider_mismatched_config_rejected():
    with pytest.raises(ValidationError):
        SemanticSearchProvider(
            id="ssp-test",
            provider=SemanticSearchProviderType.PGVECTOR,
            config=PgVectorScaleConfig(
                hostname="h",
                username="u",
                password=SecretStr("p"),
                database="db",
                pool=PoolConfig(),
            ),
        )


def test_semantic_search_provider_mismatched_config_rejected_pgvectorscale():
    with pytest.raises(ValidationError):
        SemanticSearchProvider(
            id="ssp-test",
            provider=SemanticSearchProviderType.PGVECTORSCALE,
            config=PgVectorConfig(
                hostname="h",
                username="u",
                password=SecretStr("p"),
                database="db",
                pool=PoolConfig(),
            ),
        )


# ===========================================================================
# LanceConfig + SemanticSearchProvider 'lance' backend
# ===========================================================================


class TestLanceConfig:
    def test_construction_minimal(self, tmp_path):
        from matrix.model.provider import LanceConfig

        cfg = LanceConfig(path=tmp_path)
        assert cfg.path == tmp_path
        assert cfg.hnsw_m == 16
        assert cfg.hnsw_ef_construction == 64
        assert cfg.hnsw_ef_search == 40
        assert cfg.index_min_rows == 1000

    def test_construction_with_overrides(self, tmp_path):
        from matrix.model.provider import LanceConfig

        cfg = LanceConfig(
            path=tmp_path,
            hnsw_m=32,
            hnsw_ef_construction=128,
            hnsw_ef_search=80,
            index_min_rows=5000,
        )
        assert cfg.path == tmp_path
        assert cfg.hnsw_m == 32
        assert cfg.hnsw_ef_construction == 128
        assert cfg.hnsw_ef_search == 80
        assert cfg.index_min_rows == 5000

    def test_path_required(self):
        from pydantic import ValidationError
        from matrix.model.provider import LanceConfig

        with pytest.raises(ValidationError):
            LanceConfig()  # type: ignore[call-arg]


class TestSemanticSearchProviderLanceBackend:
    def test_lance_row_round_trip(self, tmp_path):
        from matrix.model.provider import (
            LanceConfig,
            SemanticSearchProvider,
            SemanticSearchProviderType,
        )

        row = SemanticSearchProvider(
            id="ssp-lance-1",
            provider=SemanticSearchProviderType.LANCE,
            config=LanceConfig(path=tmp_path),
        )
        assert row.provider == SemanticSearchProviderType.LANCE
        assert isinstance(row.config, LanceConfig)
        # JSON round-trip survives the discriminator.
        dumped = row.model_dump(mode="json")
        round_trip = SemanticSearchProvider.model_validate(dumped)
        assert round_trip == row

    def test_lance_provider_with_pgvector_config_rejected(self, tmp_path):
        from pydantic import ValidationError
        from matrix.model.provider import (
            PgVectorConfig,
            SemanticSearchProvider,
            SemanticSearchProviderType,
        )

        with pytest.raises(ValidationError) as ei:
            SemanticSearchProvider(
                id="bad",
                provider=SemanticSearchProviderType.LANCE,
                config=PgVectorConfig(
                    hostname="x", port=5432, username="u",
                    password="p", database="d",
                ),
            )
        assert "LanceConfig" in str(ei.value)

    def test_pgvector_provider_with_lance_config_rejected(self, tmp_path):
        from pydantic import ValidationError
        from matrix.model.provider import (
            LanceConfig,
            SemanticSearchProvider,
            SemanticSearchProviderType,
        )

        with pytest.raises(ValidationError) as ei:
            SemanticSearchProvider(
                id="bad",
                provider=SemanticSearchProviderType.PGVECTOR,
                config=LanceConfig(path=tmp_path),
            )
        assert "PgVectorConfig" in str(ei.value)


class TestVectorStoreProviderConfigLanceBackend:
    """The internal VectorStoreProviderConfig adapter accepts the same
    backend kinds as the public SemanticSearchProvider; verify the
    LANCE branch validates symmetrically."""

    def test_lance_config_round_trip(self, tmp_path):
        from matrix.model.provider import (
            LanceConfig,
            VectorStoreProviderConfig,
            VectorStoreProviderType,
        )

        cfg = VectorStoreProviderConfig(
            provider=VectorStoreProviderType.LANCE,
            config=LanceConfig(path=tmp_path),
        )
        assert cfg.provider == VectorStoreProviderType.LANCE
        assert isinstance(cfg.config, LanceConfig)

    def test_lance_provider_with_pgvector_config_rejected(self):
        from pydantic import ValidationError
        from matrix.model.provider import (
            PgVectorConfig,
            VectorStoreProviderConfig,
            VectorStoreProviderType,
        )

        with pytest.raises(ValidationError) as ei:
            VectorStoreProviderConfig(
                provider=VectorStoreProviderType.LANCE,
                config=PgVectorConfig(
                    hostname="x", port=5432, username="u",
                    password="p", database="d",
                ),
            )
        assert "LanceConfig" in str(ei.value)
