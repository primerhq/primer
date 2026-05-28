"""Application configuration loaded from env vars (and an optional TOML file).

Carries DB connection parameters (via a provider-factory shape) plus
HTTP/scheduler/worker knobs. Provider rows, toolset rows, vector-store
rows all live in storage and are managed via the API itself.

Every field has a default. The zero-config path — no env vars, no
TOML, no init args — constructs successfully and is interpreted by
``matrix.api.app._build_storage_provider`` as "embedded SQLite at
``~/.matrix/db/data.sqlite``".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from matrix.model.provider import StorageProviderConfig
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

    # --- Storage ---------------------------------------------------------
    db: StorageProviderConfig | None = Field(
        default=None,
        description=(
            "Entity-storage backend. None (default) means 'use embedded "
            "SQLite at ~/.matrix/db/data.sqlite'. Set to a "
            "StorageProviderConfig with provider='postgres' (or 'sqlite' "
            "with a custom path) to override."
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
            "manually via the API or 'matrix init'."
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

        TOML path is read from ``$MATRIX_CONFIG_PATH`` at instantiation
        time. The CLI's YAML loader feeds its parsed dict through
        ``init_settings`` so a CLI-supplied YAML wins over env vars.
        """
        toml_path = os.environ.get("MATRIX_CONFIG_PATH") or None
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if toml_path is not None:
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=toml_path))
        sources.extend([dotenv_settings, file_secret_settings])
        return tuple(sources)


__all__ = ["AppConfig"]
