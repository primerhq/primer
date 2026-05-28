"""Application configuration loaded from env vars (and an optional TOML file).

Carries DB connection parameters (via a provider-factory shape) plus
HTTP/scheduler/worker knobs. Provider rows, toolset rows, vector-store
rows all live in storage and are managed via the API itself.

Every field has a default. The zero-config path — no env vars, no
TOML, no init args — constructs successfully and is interpreted by
``primer.api.app._build_storage_provider`` as "embedded SQLite at
``~/.primer/db/data.sqlite``".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from primer.model.provider import StorageProviderConfig
from primer.model.scheduler import (
    RuntimeMode,
    SchedulerProviderConfig,
    WorkerConfig,
)


class ObservabilityConfig(BaseModel):
    """Configuration for OTEL tracing + Prometheus metrics."""

    enabled: bool = True
    traces_enabled: bool = True
    metrics_enabled: bool = True
    trace_llm_io: bool = False
    otlp_endpoint: str | None = None
    otlp_headers: dict[str, str] = Field(default_factory=dict)
    service_name: str = "primer"
    service_namespace: str = "default"


class AuthConfig(BaseModel):
    """Cookie-based session authentication.

    ``session_secret`` priority:
    1. PRIMER_SESSION_SECRET env var (set this field via AppConfig
       env-loading); operator owns rotation.
    2. system_state.session_secret column (auto-generated on first
       need by the auth layer).

    ``cookie_secure`` defaults to False so dev installs work over http.
    Production deployments behind TLS should set it True to make the
    browser refuse to send the cookie over a non-https connection."""

    enabled: bool = True
    session_secret: str | None = None
    session_ttl_days: int = 7
    cookie_name: str = "primer_session"
    cookie_secure: bool = False
    cookie_samesite: str = "lax"


class AppConfig(BaseSettings):
    """Lightweight app-level configuration."""

    model_config = SettingsConfigDict(
        env_prefix="PRIMER_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- Storage ---------------------------------------------------------
    db: StorageProviderConfig | None = Field(
        default=None,
        description=(
            "Entity-storage backend. None (default) means 'use embedded "
            "SQLite at ~/.primer/db/data.sqlite'. Set to a "
            "StorageProviderConfig with provider='postgres' (or 'sqlite' "
            "with a custom path) to override."
        ),
    )
    db_schema: str | None = Field(
        default=None,
        description=(
            "Override the Postgres schema used by the storage provider. "
            "Applies only when the backend is Postgres; has no effect on "
            "SQLite (which has no schema concept). Intended for test "
            "isolation: set PRIMER_DB_SCHEMA=<name> to place all tables "
            "in a dedicated schema so concurrent test runs don't collide."
        ),
    )

    # --- HTTP server -----------------------------------------------------
    host: str = Field(default="0.0.0.0", description="Bind host for uvicorn.")
    port: int = Field(default=8000, description="Bind port for uvicorn.")

    # --- Background execution (scheduler + worker pool) ------------------
    runtime_mode: RuntimeMode = Field(
        default=RuntimeMode.API_PLUS_WORKER,
        description=(
            "What this process should do: 'api' / 'worker' / 'api+worker'. "
            "See docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md."
        ),
    )
    scheduler: SchedulerProviderConfig | None = Field(
        default=None,
        description=(
            "Scheduler backend. None (default) means 'use the in-memory "
            "scheduler' — appropriate for single-process embedded use. "
            "Set provider='postgres' for production deployments."
        ),
    )
    worker: WorkerConfig = Field(
        default_factory=WorkerConfig,
        description="Worker pool knobs (concurrency, lease TTL, etc.).",
    )

    # --- MCP toolset stdio safety ----------------------------------------
    mcp_stdio_allowed_commands: list[str] | None = Field(
        default=None,
        description=(
            "Safelist of executable names that an MCP Toolset with "
            "transport='stdio' is allowed to launch. None disables the "
            "check."
        ),
    )

    # --- Bootstrap -------------------------------------------------------
    auto_bootstrap: bool = Field(
        default=True,
        description=(
            "If True (default), run the first-boot auto-bootstrap on "
            "lifespan start when system_state.bootstrap_completed_at IS NULL. "
            "Set to False to skip auto-bootstrap and provision providers "
            "manually via the API or 'primer init'."
        ),
    )

    # --- Observability ---------------------------------------------------
    observability: ObservabilityConfig = Field(
        default_factory=ObservabilityConfig,
        description=(
            "OTEL tracing + Prometheus metrics configuration. "
            "Set enabled=False to disable all observability overhead."
        ),
    )

    # --- Auth ------------------------------------------------------------
    auth: AuthConfig = Field(
        default_factory=AuthConfig,
        description=(
            "Cookie-based session auth. Single-user in v1; the first "
            "POST /v1/auth/register creates the operator account. "
            "Subsequent boots require login. Set auth.enabled=False to "
            "disable the middleware entirely (development only)."
        ),
    )

    # --- Misc ------------------------------------------------------------
    log_level: Literal["debug", "info", "warning", "error"] = Field(
        default="info",
        description="Log level for application + uvicorn access logs.",
    )
    log_file: Path | None = Field(
        default=None,
        description=(
            "If set, application logs are written to this file (rotated). "
            "None keeps stdout/stderr behaviour."
        ),
    )
    log_json: bool = Field(
        default=True,
        description=(
            "True (default) emits one JSON object per log line. "
            "False uses a human-readable single-line formatter."
        ),
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Source priority: init args > env > TOML > .env > secrets file.

        TOML path is read from ``$PRIMER_CONFIG_PATH`` at instantiation
        time. The CLI's YAML loader feeds its parsed dict through
        ``init_settings`` so a CLI-supplied YAML wins over env vars.
        """
        toml_path = os.environ.get("PRIMER_CONFIG_PATH") or None
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if toml_path is not None:
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=toml_path))
        sources.extend([dotenv_settings, file_secret_settings])
        return tuple(sources)


__all__ = ["AppConfig", "ObservabilityConfig"]
