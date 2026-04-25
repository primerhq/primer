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
from pydantic import ValidationError

from matrix.model.common import Identifiable
from matrix.model.provider import (
    HttpConfig,
    McpConfig,
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
