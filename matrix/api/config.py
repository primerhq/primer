"""Application configuration loaded from env vars (and an optional TOML file).

Carries database connection parameters only. Provider configs, vector
store config, toolset config — all live in storage rows and are
managed via the API itself. The lifespan handler in
:mod:`matrix.api.app` builds :class:`StorageProvider` from this and
seeds two empty registries that lazy-instantiate adapters from rows
on demand.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from matrix.model.provider import VectorStoreProviderConfig
from matrix.model.scheduler import (
    RuntimeMode,
    SchedulerProviderConfig,
    WorkerConfig,
)


class AppConfig(BaseSettings):
    """Lightweight app-level configuration."""

    model_config = SettingsConfigDict(
        env_prefix="MATRIX_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- Storage (Postgres only in v1) -----------------------------------
    db_host: str = Field(..., description="Postgres host.")
    db_port: int = Field(default=5432, description="Postgres port.")
    db_database: str = Field(..., description="Postgres database name.")
    db_user: str = Field(..., description="Postgres user.")
    db_password: SecretStr = Field(..., description="Postgres password.")
    db_min_pool_size: int = Field(
        default=1, ge=1, description="Lower bound for the connection pool."
    )
    db_max_pool_size: int = Field(
        default=10, ge=1, description="Upper bound for the connection pool."
    )

    # --- HTTP server -----------------------------------------------------
    host: str = Field(default="0.0.0.0", description="Bind host for uvicorn.")
    port: int = Field(default=8000, description="Bind port for uvicorn.")

    # --- Vector store (Phase-3 infrastructure) ---------------------------
    # Single active vector store backing the internal collections
    # subsystem. Required for Collection / Document / search routes to
    # work; ``None`` means the subsystem is disabled. Discriminated
    # union of pgvector / pgvectorscale (see
    # :class:`matrix.model.provider.VectorStoreProviderConfig`).
    #
    # Env shape (uses the BaseSettings nested-delimiter ``__``):
    #   MATRIX_VECTOR_STORE__PROVIDER=pgvector
    #   MATRIX_VECTOR_STORE__CONFIG__HOSTNAME=localhost
    #   MATRIX_VECTOR_STORE__CONFIG__PORT=5432
    #   ... etc.
    # Or via TOML at ``$MATRIX_CONFIG_PATH``:
    #   [vector_store]
    #   provider = "pgvector"
    #   [vector_store.config]
    #   hostname = "..."
    vector_store: VectorStoreProviderConfig | None = Field(
        default=None,
        description=(
            "Vector store backend configuration (pgvector or "
            "pgvectorscale). Required for the internal collections "
            "subsystem. ``None`` disables collection / search "
            "functionality at the API layer."
        ),
    )

    # --- Background execution (scheduler + worker pool) ------------------
    runtime_mode: RuntimeMode = Field(
        default=RuntimeMode.API_PLUS_WORKER,
        description=(
            "What this process should do: serve HTTP only ('api'), run "
            "the worker pool only ('worker'), or both ('api+worker'). "
            "See docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md "
            "§9 for the full lifespan-wiring contract."
        ),
    )
    scheduler: SchedulerProviderConfig | None = Field(
        default=None,
        description=(
            "Scheduler backend. REQUIRED when runtime_mode != 'api'. "
            "Use 'postgres' for production (lease columns + LISTEN/NOTIFY), "
            "'in_memory' for single-process dev or tests."
        ),
    )
    worker: WorkerConfig = Field(
        default_factory=WorkerConfig,
        description=(
            "Worker pool knobs (concurrency, lease TTL, heartbeat cadence, "
            "retry policy). Ignored when runtime_mode == 'api'."
        ),
    )

    # --- MCP toolset stdio safety ----------------------------------------
    mcp_stdio_allowed_commands: list[str] | None = Field(
        default=None,
        description=(
            "Safelist of executable names that an MCP Toolset row with "
            "transport='stdio' is allowed to launch. ``None`` (the "
            "default) disables the check, which is acceptable when "
            "Toolset creation is operator-restricted; in any "
            "multi-tenant or otherwise less-trusted deployment this "
            "MUST be set to a tight allowlist (e.g. ['python', 'node']) "
            "or stdio toolsets MUST be disabled at the upstream auth "
            "layer."
        ),
    )

    # --- Misc ------------------------------------------------------------
    log_level: Literal["debug", "info", "warning", "error"] = Field(
        default="info",
        description="Log level for both application logs and uvicorn access logs.",
    )
    log_file: Path | None = Field(
        default=None,
        description=(
            "When set, application logs are written to this file (rotated "
            "at 10 MB, 5 backups) instead of stderr. The parent directory "
            "is created on demand. ``None`` (the default) keeps the "
            "stdout/stderr behaviour."
        ),
    )
    log_json: bool = Field(
        default=True,
        description=(
            "When True (default) emit one JSON object per log line — "
            "suitable for aggregators. Set False for the human-readable "
            "single-line dev formatter."
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

        TOML file path is read from ``$MATRIX_CONFIG_PATH`` at
        instantiation time (not at class-definition time) so tests
        that ``monkeypatch.setenv`` see the right value.
        """
        toml_path = os.environ.get("MATRIX_CONFIG_PATH") or None
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if toml_path is not None:
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=toml_path))
        sources.extend([dotenv_settings, file_secret_settings])
        return tuple(sources)


__all__ = ["AppConfig"]
