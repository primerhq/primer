"""Storage provider configuration (postgres / sqlite).

Defines the generic CRUD + predicate-search Storage provider configs.
``PoolConfig`` and ``_PostgresBaseConfig`` are defined here and reused by
the vector (pgvector-family) provider configs.
"""

from __future__ import annotations

from pathlib import Path
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, PositiveInt, SecretStr, model_validator


class StorageProviderType(str, Enum):
    """Supported Storage provider backends."""

    POSTGRES = "postgres"
    SQLITE = "sqlite"


class PoolConfig(BaseModel):
    """Connection pool settings shared by Postgres-backed providers.

    Maps directly onto asyncpg's :func:`asyncpg.create_pool` parameters.
    Defaults are tuned for a small-to-medium application; large
    deployments should raise ``max_size`` to match expected concurrency.
    """

    min_size: PositiveInt = Field(
        default=1,
        description="Minimum number of connections kept open in the pool.",
    )
    max_size: PositiveInt = Field(
        default=25,
        description=(
            "Maximum number of connections the pool will open. asyncpg opens "
            "connections lazily up to this ceiling and closes idle ones, so a "
            "high ceiling costs nothing for a quiet process. The default "
            "accounts for a worker/coordinator process, which pins several "
            "long-lived LISTEN connections from this same pool (scheduler "
            "session_ready + session_cancel, claim engine claim_ready, and one "
            "per event-bus subscriber: yield listener, session/chat tick "
            "forwarders, mcp-task bridge, watchers) on top of per-turn storage "
            "and rate-limiter acquires at worker concurrency. With the old "
            "default of 10 those persistent listeners starved per-turn acquires "
            "and a turn could block on pool.acquire. Large deployments should "
            "raise this further to match expected concurrency."
        ),
    )
    acquire_timeout: float = Field(
        default=30.0,
        gt=0,
        description="Seconds a caller will wait to acquire a connection before raising.",
    )
    max_idle: float = Field(
        default=300.0,
        gt=0,
        description="Seconds an idle connection may stay in the pool before being closed.",
    )
    max_lifetime: float = Field(
        default=3600.0,
        gt=0,
        description="Seconds a connection may live before being recycled (defends against leaks).",
    )

    @model_validator(mode="after")
    def _validate_sizes(self) -> "PoolConfig":
        if self.max_size < self.min_size:
            raise ValueError(
                f"max_size ({self.max_size}) must be >= min_size ({self.min_size})"
            )
        return self


class _PostgresBaseConfig(BaseModel):
    """Common Postgres connection fields shared by every Postgres-backed provider.

    Note ``db_schema`` rather than ``schema`` -- ``schema`` would shadow
    Pydantic's deprecated ``BaseModel.schema()`` method.
    """

    hostname: str = Field(
        ...,
        min_length=1,
        description="Postgres host (e.g. 'db.internal' or '127.0.0.1').",
    )
    port: int = Field(
        default=5432,
        ge=1,
        le=65535,
        description="Postgres TCP port.",
    )
    username: str = Field(
        ...,
        min_length=1,
        description="Postgres role to authenticate as.",
    )
    password: SecretStr = Field(
        ...,
        description="Password for the role.",
    )
    database: str = Field(
        ...,
        min_length=1,
        description="Database name to connect to.",
    )
    db_schema: str = Field(
        default="public",
        min_length=1,
        description=(
            "Postgres schema where tables and indexes are created. Renamed "
            "from 'schema' to avoid shadowing Pydantic's BaseModel.schema()."
        ),
    )
    pool: PoolConfig = Field(
        default_factory=PoolConfig,
        description="Connection pool settings.",
    )


class PostgresConfig(_PostgresBaseConfig):
    """Connection settings for the plain Postgres Storage provider.

    No vector extensions required; suitable for the generic CRUD +
    predicate-search :class:`Storage` interface backed by JSONB tables.
    """


class SqliteConfig(BaseModel):
    """Connection settings for the embedded SQLite Storage provider.

    Single-file backend. aiosqlite serialises queries through one
    connection so we expose no pool knobs. WAL mode is the
    recommended default — concurrent readers + single writer.
    """

    path: Path = Field(
        ...,
        description=(
            "Filesystem path to the SQLite database file. Parent "
            "directories are created on demand at initialize() "
            "time. Use a '.sqlite' or '.db' extension by convention."
        ),
    )
    busy_timeout_ms: int = Field(
        default=5000,
        ge=0,
        description=(
            "PRAGMA busy_timeout in milliseconds — how long to wait "
            "when another writer holds the lock before raising "
            "SQLITE_BUSY. 5000 is generous for embedded use."
        ),
    )
    synchronous: Literal["off", "normal", "full"] = Field(
        default="normal",
        description=(
            "PRAGMA synchronous mode. 'normal' is the WAL-recommended "
            "default (one fsync per checkpoint). 'full' = one fsync "
            "per transaction. 'off' = no fsync (risk DB corruption "
            "on power loss)."
        ),
    )
    journal_mode: Literal["wal", "delete", "truncate", "memory"] = Field(
        default="wal",
        description=(
            "PRAGMA journal_mode. 'wal' is the recommended default. "
            "'memory' is for ephemeral test DBs only."
        ),
    )


class StorageProviderConfig(BaseModel):
    """Top-level Storage provider configuration -- discriminated by ``provider``."""

    provider: StorageProviderType = Field(
        ...,
        description="Which Storage backend to use.",
    )
    config: PostgresConfig | SqliteConfig = Field(
        ...,
        description="Backend-specific connection settings; must match ``provider``.",
    )

    @model_validator(mode="after")
    def _validate_config_matches(self) -> "StorageProviderConfig":
        if self.provider == StorageProviderType.POSTGRES and not isinstance(
            self.config, PostgresConfig
        ):
            raise ValueError(
                "provider='postgres' requires a PostgresConfig in 'config'"
            )
        if self.provider == StorageProviderType.SQLITE and not isinstance(
            self.config, SqliteConfig
        ):
            raise ValueError(
                "provider='sqlite' requires a SqliteConfig in 'config'"
            )
        return self
