"""Toolset provider configuration (tool sources: internal registry or MCP server).

Defines :class:`Toolset` and the MCP transport / OAuth configs it can
carry. A toolset is conceptually a *set of tools* the application can
offer to LLMs; the tools themselves are resolved at runtime from the
provider.
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator, model_validator

from primer.model.common import Identifiable
from primer.model.masked_url import MaskedUserinfoStr


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
    """How an HTTP MCP server signs in with OAuth.

    Attached to :class:`HttpConfig` via the optional ``oauth`` field. The block DESCRIBES the sign-in and performs none: nothing happens on a 401 by
    itself. A toolset is connected in a separate, explicit step, and the sign-in belongs to the toolset: one per toolset, shared by every agent that
    uses it, never one per user.

    ``grant`` picks the sign-in. ``authorization_code`` is a person approving access at the provider's consent page (PKCE, no secret needed unless the
    provider pre-registered a confidential client). ``client_credentials`` is the primer server exchanging a client id and secret for a token at the
    token endpoint, with nobody present; it needs both, and may name the token endpoint instead of discovering it.
    """

    grant: Literal["authorization_code", "client_credentials"] = Field(
        default="authorization_code",
        description=(
            "How the toolset signs in. 'authorization_code': a person approves access in a browser. 'client_credentials': the server "
            "exchanges static_client's id and secret for a token itself (both are required)."
        ),
    )
    scopes: list[str] = Field(
        default_factory=list,
        description="OAuth scopes to request. Empty list = whatever the server defaults to.",
    )
    resource_uri: str | None = Field(
        default=None,
        description=(
            "RFC 8707 resource indicator, sent with the sign-in and token requests when the server publishes protected-resource metadata. "
            "If None, defaults to the MCP server's full URL (the spec's canonical resource identifier). Must be that URL or a parent of it "
            "on the same origin (scheme, host and port): a write that names another origin or a sibling path is refused."
        ),
    )
    static_client: OAuthClientCredentials | None = Field(
        default=None,
        description=(
            "If set, skip Dynamic Client Registration and use these "
            "credentials. Required when the auth server does not "
            "support DCR; optional otherwise. With grant='client_credentials' "
            "the client_secret is required too."
        ),
    )
    token_endpoint: str | None = Field(
        default=None,
        description=(
            "Client credentials only: the URL the client id and secret are sent to. If None, it is discovered from the MCP server's "
            "authorization server. An http(s) URL with a host and no credentials in it. The stored client secret is kept only for the "
            "origin (scheme, host and port) of this URL."
        ),
    )
    client_name: str = Field(
        default="primer",
        description="client_name passed during DCR; appears in consent UIs.",
    )

    @field_validator("token_endpoint")
    @classmethod
    def _token_endpoint_is_an_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname or any(ch.isspace() or ord(ch) < 32 for ch in value):
            raise ValueError("token_endpoint must be an http or https URL with a host")
        if parts.username is not None or parts.password is not None:
            raise ValueError("token_endpoint must not carry credentials in the URL: the client id and secret are sent separately")
        return value

    @model_validator(mode="after")
    def _grant_has_what_it_needs(self) -> "OAuthConfig":
        if self.grant == "client_credentials":
            if self.static_client is None:
                raise ValueError("grant='client_credentials' requires static_client (the client id and secret to exchange)")
            secret = self.static_client.client_secret
            if secret is None or not secret.get_secret_value():
                raise ValueError("grant='client_credentials' requires static_client.client_secret")
        elif self.token_endpoint is not None:
            raise ValueError("token_endpoint applies to client credentials only")
        return self


class ToolsetProviderType(str, Enum):
    """Supported toolset provider backends.

    The string value is what gets serialized in configuration files, so it
    must remain stable across releases.
    """

    INTERNAL = "internal"
    MCP = "mcp"
    PYTHON = "python"


class TransportType(str, Enum):
    """Transport mechanisms supported by the MCP toolset provider."""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"


class StdioConfig(BaseModel):
    """Stdio transport for an MCP server — launches a subprocess and
    speaks the MCP protocol over its stdin/stdout.
    """

    command: list[str] = Field(
        ...,
        min_length=1,
        description="Command (argv) to launch the MCP server subprocess.",
    )
    env: dict[str, SecretStr] = Field(
        default_factory=dict,
        description=(
            "Environment variables to set when launching the subprocess. "
            "Values are secrets (they routinely carry API keys for the MCP "
            "server): they are masked on every API read path and only "
            "unwrapped at the subprocess boundary."
        ),
    )


class HttpConfig(BaseModel):
    """HTTP-based connection details for a remote MCP server endpoint.

    Shared by two wire protocols, selected via ``McpConfig.transport``:
    ``http`` (modern streamable HTTP) and ``sse`` (legacy HTTP+SSE). Both use
    the same url / headers / oauth; only the client wire protocol differs.
    """

    url: MaskedUserinfoStr = Field(
        ...,
        min_length=1,
        description="Base URL of the remote MCP server endpoint.",
    )
    headers: dict[str, SecretStr] = Field(
        default_factory=dict,
        description=(
            "HTTP headers to include on every request to the MCP server "
            "(e.g. Authorization). Values are secrets (they routinely carry "
            "bearer tokens): they are masked on every API read path and only "
            "unwrapped when the request is issued."
        ),
    )
    oauth: OAuthConfig | None = Field(
        default=None,
        description=(
            "If set, the server signs in with OAuth (see OAuthConfig). The block only describes the sign-in: nothing happens on a 401 by itself."
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
        if self.transport in (
            TransportType.HTTP,
            TransportType.SSE,
        ) and not isinstance(self.config, HttpConfig):
            raise ValueError(
                "transport='http'/'sse' requires an HttpConfig in 'config'"
            )
        return self


class PythonConfig(BaseModel):
    """Config for a ``python`` toolset: one module's source plus its limits.

    The whole toolset is a single module. Every function in it decorated with
    ``@primer_tool`` becomes a tool; see
    :mod:`primer.toolset.python_runner.registration`.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        ...,
        description="Python module source. Not a secret; returned on read.",
    )
    source_version: int = Field(
        ...,
        ge=1,
        description=(
            "Bumped on every source edit. Stamped into a park's resume "
            "metadata so a resume runs the version that parked, not whatever "
            "the source says now."
        ),
    )
    default_timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        le=300.0,
        description=(
            "Wall-clock ceiling for a tool that does not declare its own. "
            "300s is the hard server ceiling."
        ),
    )
    env: dict[str, SecretStr] = Field(
        default_factory=dict,
        description=(
            "Environment variables for the runner process. Secrets: masked on "
            "every API read path, same as the MCP stdio env."
        ),
    )
    image: str | None = Field(
        default=None,
        description=(
            "Container image for the runner on container/k8s backends. None "
            "uses the default runner image. Ignored by the local runner, "
            "which is stdlib-only by construction."
        ),
    )
    allow_network: bool = Field(
        default=False,
        description=(
            "Permit outbound sockets from the tool. Enforceable only at "
            "isolation levels above 'rlimit-only'."
        ),
    )


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
    config: McpConfig | PythonConfig | None = Field(
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
        if self.provider == ToolsetProviderType.MCP and not isinstance(
            self.config, McpConfig
        ):
            raise ValueError(
                "provider='mcp' requires a McpConfig in 'config'"
            )
        if self.provider == ToolsetProviderType.PYTHON and not isinstance(
            self.config, PythonConfig
        ):
            raise ValueError(
                "provider='python' requires a PythonConfig in 'config'"
            )
        if self.provider == ToolsetProviderType.INTERNAL and self.config is not None:
            raise ValueError(
                "provider='internal' must not have a 'config'"
            )
        return self
