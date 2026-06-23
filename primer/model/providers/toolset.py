"""Toolset provider configuration (tool sources: internal registry or MCP server).

Defines :class:`Toolset` and the MCP transport / OAuth configs it can
carry. A toolset is conceptually a *set of tools* the application can
offer to LLMs; the tools themselves are resolved at runtime from the
provider.
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, HttpUrl, SecretStr, model_validator

from primer.model.common import Identifiable


class OAuthClientCredentials(BaseModel):
    """Static OAuth client credentials, for servers that don't support DCR.

    If ``client_secret`` is None the client is treated as a public client
    (PKCE only, no Authorization header on the token endpoint). If
    ``client_secret`` is set the token endpoint is called with HTTP Basic.
    """

    client_id: str = Field(
        ...,
        min_length=1,
        description="OAuth client identifier.",
    )
    client_secret: SecretStr | None = Field(
        default=None,
        description="OAuth client secret. None for public PKCE-only clients.",
    )


class OAuthConfig(BaseModel):
    """OAuth settings for an HTTP MCP server.

    Attached to :class:`HttpConfig` via the optional ``oauth`` field.
    When present, :class:`primer.toolset.mcp.McpToolsetProvider`
    performs Authorization Code + PKCE on a 401 from the MCP endpoint
    instead of immediately failing.
    """

    redirect_uri: HttpUrl = Field(
        ...,
        description=(
            "The OAuth callback URL the application exposes. The auth "
            "server will redirect the user here with ?code=...&state=...; "
            "the application's handler then calls "
            "McpToolsetProvider.complete_oauth(code=..., state=...)."
        ),
    )
    scopes: list[str] = Field(
        default_factory=list,
        description="OAuth scopes to request. Empty list = whatever the server defaults to.",
    )
    resource_uri: str | None = Field(
        default=None,
        description=(
            "RFC 8707 resource indicator. Set on token requests when "
            "the negotiated spec version is 2025-06-18 or 2025-11-25. "
            "If None, defaults to the MCP server's URL (the spec's "
            "recommended canonical resource identifier)."
        ),
    )
    static_client: OAuthClientCredentials | None = Field(
        default=None,
        description=(
            "If set, skip Dynamic Client Registration and use these "
            "credentials. Required when the auth server does not "
            "support DCR; optional otherwise."
        ),
    )
    spec_version: Literal["2025-03-26", "2025-06-18", "2025-11-25"] | None = Field(
        default=None,
        description=(
            "Force a specific MCP authorization spec version. If None, "
            "the adapter probes (newest -> oldest) and picks the first "
            "version the server supports."
        ),
    )
    client_name: str = Field(
        default="primer",
        description="client_name passed during DCR; appears in consent UIs.",
    )


class ToolsetProviderType(str, Enum):
    """Supported toolset provider backends.

    The string value is what gets serialized in configuration files, so it
    must remain stable across releases.
    """

    INTERNAL = "internal"
    MCP = "mcp"


class TransportType(str, Enum):
    """Transport mechanisms supported by the MCP toolset provider."""

    STDIO = "stdio"
    HTTP = "http"


class StdioConfig(BaseModel):
    """Stdio transport for an MCP server — launches a subprocess and
    speaks the MCP protocol over its stdin/stdout.
    """

    command: list[str] = Field(
        ...,
        min_length=1,
        description="Command (argv) to launch the MCP server subprocess.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables to set when launching the subprocess.",
    )


class HttpConfig(BaseModel):
    """HTTP transport for an MCP server — connects to a remote endpoint
    speaking MCP over HTTP (e.g. SSE / streamable HTTP).
    """

    url: str = Field(
        ...,
        min_length=1,
        description="Base URL of the remote MCP server endpoint.",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="HTTP headers to include on every request to the MCP server (e.g. Authorization).",
    )
    oauth: OAuthConfig | None = Field(
        default=None,
        description=(
            "If set, the adapter performs OAuth 2.1 (PKCE) on 401 "
            "responses. Any Authorization header in `headers` is "
            "overridden by the OAuth flow's bearer token once obtained."
        ),
    )


class McpConfig(BaseModel):
    """Connection settings for an MCP toolset provider.

    Carries a :attr:`transport` discriminator selecting how the client
    talks to the MCP server, and a :attr:`config` field holding the
    transport-specific details. The two MUST agree — a model validator
    enforces consistency.
    """

    transport: TransportType = Field(
        ...,
        description="Which transport mechanism the MCP client uses to talk to the server.",
    )
    config: StdioConfig | HttpConfig = Field(
        ...,
        description="Transport-specific connection details. Must match ``transport``.",
    )

    @model_validator(mode="after")
    def _validate_config_matches_transport(self) -> "McpConfig":
        if self.transport == TransportType.STDIO and not isinstance(self.config, StdioConfig):
            raise ValueError(
                "transport='stdio' requires a StdioConfig in 'config'"
            )
        if self.transport == TransportType.HTTP and not isinstance(self.config, HttpConfig):
            raise ValueError(
                "transport='http' requires an HttpConfig in 'config'"
            )
        return self


class Toolset(Identifiable):
    """A configured tool source.

    Conceptually a *set of tools* the application can offer to LLMs.
    Tools themselves are not enumerated here — they are resolved at
    runtime from the provider:

    * ``internal`` — tools registered in the application's internal
      registry (looked up by toolset ``id``). No additional config is
      needed; ``config`` MUST be ``None``.
    * ``mcp`` — tools queried from an MCP server. ``config`` MUST be an
      :class:`McpConfig` describing the transport and its connection
      details.

    The ``id`` (inherited from :class:`Identifiable`) is a user-chosen
    handle the application uses to refer to this toolset.
    """

    _id_prefix: ClassVar[str] = "toolset"

    provider: ToolsetProviderType = Field(
        ...,
        description="Which toolset provider backend this entry targets.",
    )
    config: McpConfig | None = Field(
        default=None,
        description="Provider-specific config. Required for 'mcp', must be omitted for 'internal'.",
    )
    harness_id: str | None = Field(
        default=None,
        description=(
            "When set, this row is managed by the named harness. "
            "Mutation through the public CRUD endpoints returns 409 — "
            "use the harness's sync/uninstall flow instead."
        ),
    )

    @model_validator(mode="after")
    def _validate_config_matches_provider(self) -> "Toolset":
        if self.provider == ToolsetProviderType.MCP and self.config is None:
            raise ValueError(
                "provider='mcp' requires a McpConfig in 'config'"
            )
        if self.provider == ToolsetProviderType.INTERNAL and self.config is not None:
            raise ValueError(
                "provider='internal' must not have a 'config'"
            )
        return self
