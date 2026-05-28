"""Tests for matrix.model.scheduler config models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.scheduler import (
    InMemorySchedulerConfig,
    PostgresSchedulerConfig,
    RuntimeMode,
    SchedulerProviderConfig,
    SchedulerProviderType,
    WorkerConfig,
)


def test_runtime_mode_values():
    assert RuntimeMode.API.value == "api"
    assert RuntimeMode.WORKER.value == "worker"
    assert RuntimeMode.API_PLUS_WORKER.value == "api+worker"


def test_worker_config_defaults():
    cfg = WorkerConfig()
    assert cfg.concurrency == 8
    assert cfg.heartbeat_interval_seconds == 10
    assert cfg.lease_ttl_seconds == 30
    assert cfg.max_attempts == 5


def test_worker_config_lease_ttl_must_exceed_2x_heartbeat():
    """The validator must reject configs that would let leases expire
    between heartbeats."""
    with pytest.raises(ValidationError):
        WorkerConfig(heartbeat_interval_seconds=20, lease_ttl_seconds=30)


def test_worker_config_lease_ttl_at_exactly_2x_is_accepted():
    cfg = WorkerConfig(heartbeat_interval_seconds=10, lease_ttl_seconds=20)
    assert cfg.lease_ttl_seconds == 20


def test_scheduler_config_postgres_round_trip():
    cfg = SchedulerProviderConfig(
        provider=SchedulerProviderType.POSTGRES,
        config=PostgresSchedulerConfig(),
    )
    again = SchedulerProviderConfig.model_validate(cfg.model_dump(mode="json"))
    assert again.provider == SchedulerProviderType.POSTGRES
    assert isinstance(again.config, PostgresSchedulerConfig)


def test_scheduler_config_in_memory_round_trip():
    cfg = SchedulerProviderConfig(
        provider=SchedulerProviderType.IN_MEMORY,
        config=InMemorySchedulerConfig(),
    )
    again = SchedulerProviderConfig.model_validate(cfg.model_dump(mode="json"))
    assert again.provider == SchedulerProviderType.IN_MEMORY


def test_postgres_scheduler_config_default_listen_reconnect():
    cfg = PostgresSchedulerConfig()
    assert cfg.listen_reconnect_seconds == 2.0
