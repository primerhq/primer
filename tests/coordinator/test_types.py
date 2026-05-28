"""Shape tests for matrix.int.coordinator ABCs and enums."""

from __future__ import annotations

import pytest

from primer.int.coordinator import (
    Coordinator,
    InvalidationBus,
    InvalidationTopic,
    LeaderElector,
    LeadershipLease,
    RateLimiter,
    RateLimiterLease,
    ROLE_TIMER_SCHEDULER,
    ROLE_TIMEOUT_SWEEPER,
    ROLE_CHAT_SWEEPER,
    ROLE_HARNESS_SWEEPER,
    ROLE_WATCHER_MANAGER,
    ROLE_MCP_BRIDGE,
    ROLE_COORDINATOR_SWEEPER,
)


def test_invalidation_topic_members():
    assert InvalidationTopic.LLM_PROVIDER.value == "llm_provider"
    assert InvalidationTopic.EMBEDDING_PROVIDER.value == "embedding_provider"
    assert InvalidationTopic.CROSS_ENCODER_PROVIDER.value == "cross_encoder_provider"
    assert InvalidationTopic.TOOLSET.value == "toolset"
    assert InvalidationTopic.SEMANTIC_SEARCH_PROVIDER.value == "semantic_search_provider"
    assert InvalidationTopic.CHANNEL_PROVIDER.value == "channel_provider"
    assert InvalidationTopic.WORKSPACE_PROVIDER.value == "workspace_provider"
    assert InvalidationTopic.HARNESS.value == "harness"


def test_role_constants_distinct():
    roles = {
        ROLE_TIMER_SCHEDULER, ROLE_TIMEOUT_SWEEPER, ROLE_CHAT_SWEEPER,
        ROLE_HARNESS_SWEEPER, ROLE_WATCHER_MANAGER, ROLE_MCP_BRIDGE,
        ROLE_COORDINATOR_SWEEPER,
    }
    assert len(roles) == 7
    assert all(isinstance(r, str) and r for r in roles)


def test_abcs_cannot_be_instantiated():
    with pytest.raises(TypeError):
        RateLimiter()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        InvalidationBus()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        LeaderElector()  # type: ignore[abstract]


def test_coordinator_dataclass_field_names():
    from dataclasses import fields
    field_names = {f.name for f in fields(Coordinator)}
    assert field_names == {"rate_limiter", "invalidation_bus", "leader_elector"}
