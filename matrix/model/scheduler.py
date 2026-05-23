"""Scheduler-related configuration models.

Three things live here:

* :class:`RuntimeMode` — what the running process should do (serve API,
  run worker pool, or both).
* :class:`WorkerConfig` — knobs for the in-process worker pool.
* :class:`SchedulerProviderConfig` — discriminated union selecting the
  :class:`Scheduler` implementation (Postgres for production,
  in-memory for tests + single-process dev).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class RuntimeMode(str, Enum):
    """What this process is responsible for."""

    API = "api"
    WORKER = "worker"
    API_PLUS_WORKER = "api+worker"


class WorkerConfig(BaseModel):
    """In-process worker pool knobs.

    The validator enforces ``lease_ttl_seconds >= 2 * heartbeat_interval_seconds``
    so a single missed heartbeat doesn't expire a lease.
    """

    concurrency: int = Field(default=8, ge=1, le=128)
    claim_batch_size: int = Field(default=4, ge=1, le=64)
    heartbeat_interval_seconds: int = Field(default=10, ge=1, le=60)
    lease_ttl_seconds: int = Field(default=30, ge=5, le=300)
    poll_interval_seconds: float = Field(default=2.0, ge=0.1, le=30.0)
    drain_timeout_seconds: int = Field(default=120, ge=1)
    max_attempts: int = Field(default=5, ge=1, le=100)
    base_backoff_seconds: float = Field(default=2.0, ge=0.1)
    max_backoff_seconds: float = Field(default=300.0, ge=1.0)

    @model_validator(mode="after")
    def _lease_ttl_at_least_2x_heartbeat(self) -> "WorkerConfig":
        if self.lease_ttl_seconds < 2 * self.heartbeat_interval_seconds:
            raise ValueError(
                f"lease_ttl_seconds ({self.lease_ttl_seconds}) must be "
                f">= 2 * heartbeat_interval_seconds "
                f"({self.heartbeat_interval_seconds}) to tolerate one "
                "missed beat"
            )
        return self


class SchedulerProviderType(str, Enum):
    POSTGRES = "postgres"
    IN_MEMORY = "in_memory"


class PostgresSchedulerConfig(BaseModel):
    """Knobs for the Postgres scheduler impl.

    Reuses the :class:`StorageProvider`'s connection pool — no DB
    parameters here. ``listen_reconnect_seconds`` controls the backoff
    when the dedicated LISTEN connection drops.
    """

    listen_reconnect_seconds: float = Field(default=2.0, ge=0.1, le=60.0)


class InMemorySchedulerConfig(BaseModel):
    """No knobs — present only for the discriminated-union shape."""


class SchedulerProviderConfig(BaseModel):
    """Discriminated config selecting the Scheduler impl.

    Mirrors the shape of :class:`matrix.model.provider.StorageProviderConfig`
    so the factory pattern is identical.
    """

    provider: SchedulerProviderType
    config: PostgresSchedulerConfig | InMemorySchedulerConfig


__all__ = [
    "InMemorySchedulerConfig",
    "PostgresSchedulerConfig",
    "RuntimeMode",
    "SchedulerProviderConfig",
    "SchedulerProviderType",
    "WorkerConfig",
]
