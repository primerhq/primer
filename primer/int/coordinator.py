"""Coordinator ABCs — see docs/superpowers/specs/2026-05-27-coordinator-design.md.

Three primitives needed for correct distributed-mode behaviour:

* :class:`RateLimiter` — global concurrency limit per provider id.
* :class:`InvalidationBus` — best-effort cross-process cache-invalidation
  broadcast.
* :class:`LeaderElector` — exactly-one-instance-runs-this-role election
  for background tasks.

Each ABC has a paired in-memory + Postgres implementation under
``primer.coordinator``. The :class:`Coordinator` dataclass bundles the
trio so call sites depend on one injectable object, not three.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import Enum


class InvalidationTopic(str, Enum):
    """The set of cache-invalidation topics the system publishes on.

    String values are used as event-key prefixes by the Postgres backend
    (``invalidate:{topic}:{entity_id}``). Adding a topic here is enough
    to make it observable to subscribers; publishers must also be
    updated to emit on the new topic.
    """

    LLM_PROVIDER = "llm_provider"
    EMBEDDING_PROVIDER = "embedding_provider"
    CROSS_ENCODER_PROVIDER = "cross_encoder_provider"
    TOOLSET = "toolset"
    SEMANTIC_SEARCH_PROVIDER = "semantic_search_provider"
    CHANNEL_PROVIDER = "channel_provider"
    WORKSPACE_PROVIDER = "workspace_provider"
    HARNESS = "harness"


ROLE_TIMER_SCHEDULER = "timer-scheduler"
ROLE_TIMEOUT_SWEEPER = "timeout-sweeper"
ROLE_CHAT_SWEEPER = "chat-sweeper"
ROLE_HARNESS_SWEEPER = "harness-sweeper"
ROLE_WATCHER_MANAGER = "watcher-manager"
ROLE_MCP_BRIDGE = "mcp-bridge"
ROLE_COORDINATOR_SWEEPER = "coordinator-sweeper"


class RateLimiterLease(AbstractAsyncContextManager["RateLimiterLease"]):
    """A single occupied slot under a :class:`RateLimiter` key.

    Held for the duration of an outbound call (LLM completion, embedding
    request, etc.). Released on context-manager exit. While held, an
    internal heartbeat task renews the underlying lease record so a
    crash bounds slot loss to one TTL window (60s default).
    """

    @abstractmethod
    async def release(self) -> None: ...

    @abstractmethod
    async def heartbeat(self) -> bool:
        """Renew the lease. Returns ``False`` if the lease was swept
        (caller should abort — the slot was reclaimed for someone else)."""


class RateLimiter(ABC):
    """Global per-key concurrency limiter."""

    @abstractmethod
    async def acquire(
        self, key: str, *, max_concurrency: int,
    ) -> RateLimiterLease: ...

    @abstractmethod
    async def try_acquire(
        self, key: str, *, max_concurrency: int, timeout_s: float,
    ) -> RateLimiterLease | None: ...


class InvalidationSubscription(ABC):
    """Handle to an active subscription. Call :meth:`aclose` to stop
    receiving events."""

    @abstractmethod
    async def aclose(self) -> None: ...


class InvalidationBus(ABC):
    """Cross-process cache-invalidation broadcast.

    Best-effort delivery: a dropped notification leaves a cache slightly
    stale but never corrupt. Subscriber handlers that raise are caught +
    logged; the subscription continues.
    """

    @abstractmethod
    async def publish(self, topic: InvalidationTopic, key: str) -> None: ...

    @abstractmethod
    async def subscribe(
        self,
        topic: InvalidationTopic,
        handler: Callable[[str], Awaitable[None]],
        *,
        on_reconnect: Callable[[], None] | None = None,
    ) -> InvalidationSubscription:
        """Subscribe ``handler`` to ``topic``.

        ``on_reconnect`` is invoked (synchronously) whenever the
        underlying transport re-establishes a dropped connection. Since
        the broadcast is not durable across a reconnect, invalidations
        emitted during the blip are lost; subscribers that cache the
        invalidated state use this hook to flush it wholesale (treat
        everything as potentially stale). Impls without a droppable
        transport never call it.
        """
        ...


@dataclass
class LeadershipLease:
    """Held by the elected instance for a role.

    The ``lost_event`` is set when the heartbeat task can no longer
    confirm ownership (partition, expired lease). Callers should race
    their work loop against ``lost_event.wait()`` and stop cleanly when
    it fires.

    ``release`` is overridden by concrete subclasses (e.g.
    in-memory and Postgres impls). The base raises NotImplementedError
    rather than using @abstractmethod, because @dataclass + ABC don't
    compose with the auto-generated __init__.
    """

    role: str
    owner_id: str
    lost_event: asyncio.Event

    async def release(self) -> None:
        raise NotImplementedError


class LeaderElector(ABC):
    """Exactly-one-instance-runs-this-role election with TTL fallback."""

    @abstractmethod
    async def try_acquire(
        self, role: str, *, lease_seconds: int = 30,
    ) -> LeadershipLease | None: ...


@dataclass
class Coordinator:
    """The three coordinator primitives, bundled.

    Constructed once per process by
    :class:`~primer.coordinator.factory.CoordinatorFactory` and stashed
    on ``app.state.coordinator``. Adapters and registries take one of
    the three as a constructor arg; tests can inject fakes per-primitive.
    """

    rate_limiter: RateLimiter
    invalidation_bus: InvalidationBus
    leader_elector: LeaderElector


__all__ = [
    "Coordinator",
    "InvalidationBus",
    "InvalidationSubscription",
    "InvalidationTopic",
    "LeaderElector",
    "LeadershipLease",
    "RateLimiter",
    "RateLimiterLease",
    "ROLE_CHAT_SWEEPER",
    "ROLE_COORDINATOR_SWEEPER",
    "ROLE_HARNESS_SWEEPER",
    "ROLE_MCP_BRIDGE",
    "ROLE_TIMEOUT_SWEEPER",
    "ROLE_TIMER_SCHEDULER",
    "ROLE_WATCHER_MANAGER",
]
