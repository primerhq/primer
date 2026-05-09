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
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
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

    # --- Misc ------------------------------------------------------------
    log_level: Literal["debug", "info", "warning", "error"] = Field(
        default="info",
        description="Log level for both application logs and uvicorn access logs.",
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
