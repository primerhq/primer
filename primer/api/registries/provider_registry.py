"""Lazy, invalidatable adapter registry for provider rows.

Holds one adapter instance per ``(LLMProvider, EmbeddingProvider,
CrossEncoderProvider, Toolset)`` row id; constructs adapters on first
read; drops the cached adapter (after calling its ``aclose``) when the
underlying row is mutated or deleted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from primer.int.coordinator import InvalidationBus, InvalidationSubscription, RateLimiter
from primer.int.cross_encoder import CrossEncoder
from primer.int.embedder import Embedder
from primer.int.llm import LLM
from primer.int.toolset import ToolsetProvider
from primer.model.except_ import ConfigError, NotFoundError
from primer.model.provider import (
    CrossEncoderProvider,
    CrossEncoderProviderType,
    EmbeddingProvider,
    EmbeddingProviderType,
    LLMProvider,
    LLMProviderType,
    Toolset,
)


if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


def _build_default_llm_factory(
    *,
    rate_limiter: "RateLimiter | None" = None,
    trace_llm_io: bool = False,
) -> Callable[[LLMProvider], LLM]:
    """Build the default ``llm_factory`` closure.

    ``rate_limiter`` is forwarded to every LLM adapter so all providers
    participate in the global concurrency limiter. ``None`` causes each
    adapter to fall back to a local :class:`~primer.coordinator.in_memory.InMemoryRateLimiter`.
    ``trace_llm_io`` controls whether prompt messages are included in spans.
    """
    def _factory(provider: LLMProvider) -> LLM:  # pragma: no cover
        match provider.provider:
            case LLMProviderType.OPENRESPONSES:
                from primer.llm.openresponses import OpenResponsesLLM
                return OpenResponsesLLM(
                    provider, rate_limiter=rate_limiter, trace_llm_io=trace_llm_io,
                )
            case LLMProviderType.OPENCHAT:
                from primer.llm.openchat import OpenChatLLM
                return OpenChatLLM(
                    provider, rate_limiter=rate_limiter, trace_llm_io=trace_llm_io,
                )
            case LLMProviderType.ANTHROPIC:
                from primer.llm.anthropic import AnthropicLLM
                return AnthropicLLM(
                    provider, rate_limiter=rate_limiter, trace_llm_io=trace_llm_io,
                )
            case LLMProviderType.GEMINI:
                from primer.llm.gemini import GeminiLLM
                return GeminiLLM(
                    provider, rate_limiter=rate_limiter, trace_llm_io=trace_llm_io,
                )
            case LLMProviderType.OLLAMA:
                from primer.llm.ollama import OllamaLLM
                return OllamaLLM(
                    provider, rate_limiter=rate_limiter, trace_llm_io=trace_llm_io,
                )
            case _:
                raise ConfigError(
                    f"unknown LLM provider type {provider.provider!r}"
                )
    return _factory


def _default_llm_factory(provider: LLMProvider) -> LLM:  # pragma: no cover
    """Dispatch on ``provider.provider`` to the right adapter constructor."""
    return _build_default_llm_factory()(provider)


def _build_default_embedder_factory(
    *,
    rate_limiter: "RateLimiter | None" = None,
) -> Callable[[EmbeddingProvider], Embedder]:
    """Build the default ``embedder_factory`` closure.

    ``rate_limiter`` is forwarded to every embedder adapter so all
    providers participate in the global concurrency limiter. ``None``
    causes each adapter to fall back to a local
    :class:`~primer.coordinator.in_memory.InMemoryRateLimiter`.
    """
    def _factory(provider: EmbeddingProvider) -> Embedder:  # pragma: no cover
        match provider.provider:
            case EmbeddingProviderType.HUGGINGFACE:
                from primer.embedder.huggingface import HuggingFaceEmbedder
                return HuggingFaceEmbedder(provider, rate_limiter=rate_limiter)
            case EmbeddingProviderType.OPENAI:
                from primer.embedder.openai import OpenAIEmbedder
                return OpenAIEmbedder(provider, rate_limiter=rate_limiter)
            case EmbeddingProviderType.GEMINI:
                from primer.embedder.gemini import GeminiEmbedder
                return GeminiEmbedder(provider, rate_limiter=rate_limiter)
            case _:
                raise ConfigError(
                    f"unknown embedding provider type {provider.provider!r}"
                )
    return _factory


def _default_embedder_factory(  # pragma: no cover
    provider: EmbeddingProvider,
) -> Embedder:
    return _build_default_embedder_factory()(provider)


def _build_default_cross_encoder_factory(
    *,
    rate_limiter: "RateLimiter | None" = None,
) -> Callable[[CrossEncoderProvider], CrossEncoder]:
    """Build the default ``cross_encoder_factory`` closure.

    ``rate_limiter`` is forwarded to every cross-encoder adapter so all
    providers participate in the global concurrency limiter. ``None``
    causes each adapter to fall back to a local
    :class:`~primer.coordinator.in_memory.InMemoryRateLimiter`.
    """
    def _factory(provider: CrossEncoderProvider) -> CrossEncoder:  # pragma: no cover
        match provider.provider:
            case CrossEncoderProviderType.HUGGINGFACE:
                from primer.cross_encoder.huggingface import HuggingFaceCrossEncoder
                return HuggingFaceCrossEncoder(provider, rate_limiter=rate_limiter)
            case _:
                raise ConfigError(
                    f"unknown cross-encoder provider type {provider.provider!r}"
                )
    return _factory


def _default_cross_encoder_factory(  # pragma: no cover
    provider: CrossEncoderProvider,
) -> CrossEncoder:
    return _build_default_cross_encoder_factory()(provider)


_SYSTEM_TOOLSET_ID = "system"
_SEARCH_TOOLSET_ID = "search"
_WORKSPACES_TOOLSET_ID = "workspaces"
_MISC_TOOLSET_ID = "misc"
# `web` has always been prefix-less; unchanged.
_WEB_TOOLSET_ID = "web"
_HARNESS_TOOLSET_ID = "harness"

# Public: ids that are always resolvable by the live registry (built-in
# providers), so external reference-integrity checks can skip the
# Toolset storage lookup for them.
RESERVED_TOOLSET_IDS: frozenset[str] = frozenset({
    _SYSTEM_TOOLSET_ID,
    _SEARCH_TOOLSET_ID,
    _WORKSPACES_TOOLSET_ID,
    _MISC_TOOLSET_ID,
    _WEB_TOOLSET_ID,
    _HARNESS_TOOLSET_ID,
})

# ---------------------------------------------------------------------------
# Reserved ids for auto-bootstrap provider kinds
#
# These ids are protected at the API layer (POST → 409, DELETE → 403).
# The factory specs that describe how to create these rows live in
# :mod:`primer.bootstrap.defaults`; the rows are upserted idempotently
# by :class:`primer.bootstrap.runner.BootstrapRunner` at first boot.
#
# Design: factories are NOT consulted at lookup time.  After bootstrap
# the row exists in storage like any other.  The reserved-id sets below
# are used exclusively by the router guards (Task 3).
# ---------------------------------------------------------------------------

# Embedding providers: id "huggingface" is the local sentence-transformers
# provider and must not be overwritten by operator POST.
RESERVED_EMBEDDER_IDS: frozenset[str] = frozenset({"huggingface"})

# Semantic-search providers: id "lance" is the local LanceDB backend.
RESERVED_SSP_IDS: frozenset[str] = frozenset({"lance"})

# Cross-encoder providers: id "huggingface-ce" is the local reranker.
RESERVED_CROSS_ENCODER_IDS: frozenset[str] = frozenset({"huggingface-ce"})

# LLM providers: no reserved ids — LLMs always require explicit operator
# provisioning (API keys); the set is defined here for symmetry so
# router guards can import a uniform name.
RESERVED_LLM_IDS: frozenset[str] = frozenset()

# Workspace providers: id "local" is the local-filesystem backend.
RESERVED_WORKSPACE_PROVIDER_IDS: frozenset[str] = frozenset({"local"})

# Backward-compat: the four `_*` ids shipped before the underscore-
# prefix convention was retired. Agents persisted before the rename
# may still reference them, so the registry maps the old ids to the
# new ones transparently on lookup. Remove this map after a deprecation
# window (when the operator-saved configs have been re-saved).
_TOOLSET_ID_ALIASES: dict[str, str] = {
    "_system": "system",
    "_workspaces": "workspaces",
    "_search": "search",
    "_misc": "misc",
}


def _build_default_toolset_factory(
    *,
    allowed_stdio_commands: frozenset[str] | None = None,
) -> Callable[[Toolset], ToolsetProvider]:
    """Build the default ``toolset_factory`` closure.

    ``allowed_stdio_commands`` is forwarded to every constructed
    :class:`McpToolsetProvider` so stdio launches are restricted to the
    operator-supplied safelist. ``None`` disables the check (acceptable
    only when Toolset creation is operator-restricted upstream).
    """
    def _factory(toolset: Toolset) -> ToolsetProvider:  # pragma: no cover
        from primer.model.provider import ToolsetProviderType

        if toolset.provider == ToolsetProviderType.MCP:
            from primer.toolset.mcp import McpToolsetProvider
            return McpToolsetProvider(
                toolset_id=toolset.id,
                config=toolset.config,
                allowed_stdio_commands=allowed_stdio_commands,
            )
        if toolset.provider == ToolsetProviderType.INTERNAL:
            raise ConfigError(
                f"toolset {toolset.id!r} declares provider='internal' "
                "but internal toolsets are constructed by the app "
                "lifespan, not from a row. Reserved internal toolset "
                "ids are: system, workspaces, search, misc, web."
            )
        raise ConfigError(
            f"unknown toolset provider type {toolset.provider!r}"
        )

    return _factory


def _default_toolset_factory(toolset: Toolset) -> ToolsetProvider:  # pragma: no cover
    """No-allowlist default. Tests use this directly; the production
    lifespan calls :func:`_build_default_toolset_factory` with the
    AppConfig allowlist instead."""
    return _build_default_toolset_factory()(toolset)


class ProviderRegistry:
    """Lazy adapter cache keyed by provider row id."""

    def __init__(
        self,
        storage_provider: "StorageProvider",
        *,
        llm_factory: Callable[[LLMProvider], LLM] | None = None,
        embedder_factory: Callable[[EmbeddingProvider], Embedder] | None = None,
        cross_encoder_factory: (
            Callable[[CrossEncoderProvider], CrossEncoder] | None
        ) = None,
        toolset_factory: Callable[[Toolset], ToolsetProvider] | None = None,
        system_toolset_provider: ToolsetProvider | None = None,
        search_toolset_provider: ToolsetProvider | None = None,
        workspaces_toolset_provider: ToolsetProvider | None = None,
        misc_toolset_provider: ToolsetProvider | None = None,
        web_toolset_provider: ToolsetProvider | None = None,
        harness_toolset_provider: ToolsetProvider | None = None,
        rate_limiter: RateLimiter | None = None,
        trace_llm_io: bool = False,
    ) -> None:
        self._sp = storage_provider
        self._rate_limiter: RateLimiter | None = rate_limiter
        self._trace_llm_io = trace_llm_io
        self._llm_factory = llm_factory or _build_default_llm_factory(
            rate_limiter=rate_limiter,
            trace_llm_io=trace_llm_io,
        )
        self._embedder_factory = embedder_factory or _build_default_embedder_factory(
            rate_limiter=rate_limiter,
        )
        self._cross_encoder_factory = (
            cross_encoder_factory or _build_default_cross_encoder_factory(
                rate_limiter=rate_limiter,
            )
        )
        self._toolset_factory = toolset_factory or _default_toolset_factory
        # Reserved id ``system`` resolves to this immutable provider
        # without consulting storage. Set via app lifespan; tests
        # may leave it None and use the row-based path.
        self._system_toolset_provider = system_toolset_provider
        # Reserved id ``search`` resolves to this provider when the
        # internal collections subsystem is active. Set lazily by the
        # subsystem bootstrap (or the lifespan handler if a config row
        # already exists at startup); ``None`` means the subsystem is
        # inactive and ``get_toolset('search')`` raises NotFoundError.
        self._search_toolset_provider = search_toolset_provider
        # Reserved id ``workspaces`` resolves to this immutable
        # provider — always built at app startup. Mirrors ``system``:
        # its tools dogfood the workspace REST API to agents.
        self._workspaces_toolset_provider = workspaces_toolset_provider
        # Reserved id ``misc`` resolves to this immutable provider —
        # always built at app startup. Stateless utilities
        # (get_datetime, sleep, uuid_v4, hash, calculate).
        self._misc_toolset_provider = misc_toolset_provider
        # Reserved id ``web`` (no underscore prefix) resolves to the
        # immutable web toolset built at app startup.
        # DuckDuckGo search + http-request primitives.
        self._web_toolset_provider = web_toolset_provider
        # Reserved id ``harness`` resolves to the harness management
        # toolset built at app startup. Agents can manage harnesses
        # (register, fetch, install, sync, uninstall) via this toolset.
        self._harness_toolset_provider = harness_toolset_provider

        self._llm_cache: dict[str, LLM] = {}
        self._embedder_cache: dict[str, Embedder] = {}
        self._cross_encoder_cache: dict[str, CrossEncoder] = {}
        self._toolset_cache: dict[str, ToolsetProvider] = {}
        self._lock = asyncio.Lock()
        self._invalidation_bus: InvalidationBus | None = None
        self._invalidation_subs: list[InvalidationSubscription] = []

    # ---- Lookups ----------------------------------------------------------

    async def get_llm(self, provider_id: str) -> LLM:
        async with self._lock:
            cached = self._llm_cache.get(provider_id)
            if cached is not None:
                return cached
            row = await self._sp.get_storage(LLMProvider).get(provider_id)
            if row is None:
                raise NotFoundError(
                    f"LLMProvider {provider_id!r} does not exist"
                )
            adapter = self._llm_factory(row)
            self._llm_cache[provider_id] = adapter
            return adapter

    async def get_embedder(self, provider_id: str) -> Embedder:
        async with self._lock:
            cached = self._embedder_cache.get(provider_id)
            if cached is not None:
                return cached
            row = await self._sp.get_storage(EmbeddingProvider).get(provider_id)
            if row is None:
                raise NotFoundError(
                    f"EmbeddingProvider {provider_id!r} does not exist"
                )
            adapter = self._embedder_factory(row)
            self._embedder_cache[provider_id] = adapter
            return adapter

    async def get_cross_encoder(self, provider_id: str) -> CrossEncoder:
        async with self._lock:
            cached = self._cross_encoder_cache.get(provider_id)
            if cached is not None:
                return cached
            row = await self._sp.get_storage(CrossEncoderProvider).get(provider_id)
            if row is None:
                raise NotFoundError(
                    f"CrossEncoderProvider {provider_id!r} does not exist"
                )
            adapter = self._cross_encoder_factory(row)
            self._cross_encoder_cache[provider_id] = adapter
            return adapter

    async def get_toolset(self, toolset_id: str) -> ToolsetProvider:
        # Transparently resolve old `_*` ids to new prefix-less ids so
        # agent rows persisted before the rename continue to work.
        toolset_id = _TOOLSET_ID_ALIASES.get(toolset_id, toolset_id)
        # Reserved id `system` short-circuits storage. Returns the
        # singleton built at app startup; immutable, never re-created.
        if (
            self._system_toolset_provider is not None
            and toolset_id == _SYSTEM_TOOLSET_ID
        ):
            return self._system_toolset_provider
        # Reserved id `search` resolves to the search toolset built
        # when the internal collections subsystem is activated.
        if (
            self._search_toolset_provider is not None
            and toolset_id == _SEARCH_TOOLSET_ID
        ):
            return self._search_toolset_provider
        # Reserved id `workspaces` resolves to the always-on
        # workspace dogfood toolset built at app startup.
        if (
            self._workspaces_toolset_provider is not None
            and toolset_id == _WORKSPACES_TOOLSET_ID
        ):
            return self._workspaces_toolset_provider
        # Reserved id `misc` resolves to the always-on misc utility
        # toolset built at app startup.
        if (
            self._misc_toolset_provider is not None
            and toolset_id == _MISC_TOOLSET_ID
        ):
            return self._misc_toolset_provider
        # Reserved id `web` resolves to the always-on web toolset
        # built at app startup.
        if (
            self._web_toolset_provider is not None
            and toolset_id == _WEB_TOOLSET_ID
        ):
            return self._web_toolset_provider
        # Reserved id `harness` resolves to the harness management
        # toolset built at app startup.
        if (
            self._harness_toolset_provider is not None
            and toolset_id == _HARNESS_TOOLSET_ID
        ):
            return self._harness_toolset_provider
        async with self._lock:
            cached = self._toolset_cache.get(toolset_id)
            if cached is not None:
                return cached
            row = await self._sp.get_storage(Toolset).get(toolset_id)
            if row is None:
                raise NotFoundError(
                    f"Toolset {toolset_id!r} does not exist"
                )
            adapter = self._toolset_factory(row)
            self._toolset_cache[toolset_id] = adapter
            return adapter

    # ---- Invalidation (private local helpers) ----------------------------

    async def _invalidate_llm_local(self, provider_id: str) -> None:
        async with self._lock:
            adapter = self._llm_cache.pop(provider_id, None)
        if adapter is not None:
            try:
                await adapter.aclose()
            except Exception as exc:  # noqa: BLE001 -- best-effort
                logger.warning(
                    "ProviderRegistry: aclose failed on LLM %r: %s",
                    provider_id,
                    exc,
                )

    async def _invalidate_embedder_local(self, provider_id: str) -> None:
        async with self._lock:
            adapter = self._embedder_cache.pop(provider_id, None)
        if adapter is not None:
            try:
                await adapter.aclose()
            except Exception as exc:  # noqa: BLE001 -- best-effort
                logger.warning(
                    "ProviderRegistry: aclose failed on Embedder %r: %s",
                    provider_id,
                    exc,
                )

    async def _invalidate_cross_encoder_local(self, provider_id: str) -> None:
        async with self._lock:
            adapter = self._cross_encoder_cache.pop(provider_id, None)
        if adapter is not None:
            try:
                await adapter.aclose()
            except Exception as exc:  # noqa: BLE001 -- best-effort
                logger.warning(
                    "ProviderRegistry: aclose failed on CrossEncoder %r: %s",
                    provider_id,
                    exc,
                )

    async def _invalidate_toolset_local(self, toolset_id: str) -> None:
        # Transparently resolve old `_*` ids to new prefix-less ids.
        toolset_id = _TOOLSET_ID_ALIASES.get(toolset_id, toolset_id)
        # The reserved internal toolsets are immutable; invalidation
        # is a no-op so the singletons survive any cascade triggered
        # by an accidental write to the reserved ids.
        if toolset_id in (
            _SYSTEM_TOOLSET_ID,
            _SEARCH_TOOLSET_ID,
            _WORKSPACES_TOOLSET_ID,
            _MISC_TOOLSET_ID,
            _WEB_TOOLSET_ID,
            _HARNESS_TOOLSET_ID,
        ):
            return
        async with self._lock:
            adapter = self._toolset_cache.pop(toolset_id, None)
        if adapter is not None:
            try:
                await adapter.aclose()
            except Exception as exc:  # noqa: BLE001 -- best-effort
                logger.warning(
                    "ProviderRegistry: aclose failed on Toolset %r: %s",
                    toolset_id,
                    exc,
                )

    # ---- Invalidation (public — routes through bus when bound) -----------

    async def invalidate_llm(self, provider_id: str) -> None:
        if self._invalidation_bus is not None:
            from primer.int.coordinator import InvalidationTopic
            await self._invalidation_bus.publish(
                InvalidationTopic.LLM_PROVIDER, provider_id,
            )
        else:
            await self._invalidate_llm_local(provider_id)

    async def invalidate_embedder(self, provider_id: str) -> None:
        if self._invalidation_bus is not None:
            from primer.int.coordinator import InvalidationTopic
            await self._invalidation_bus.publish(
                InvalidationTopic.EMBEDDING_PROVIDER, provider_id,
            )
        else:
            await self._invalidate_embedder_local(provider_id)

    async def invalidate_cross_encoder(self, provider_id: str) -> None:
        if self._invalidation_bus is not None:
            from primer.int.coordinator import InvalidationTopic
            await self._invalidation_bus.publish(
                InvalidationTopic.CROSS_ENCODER_PROVIDER, provider_id,
            )
        else:
            await self._invalidate_cross_encoder_local(provider_id)

    async def invalidate_toolset(self, toolset_id: str) -> None:
        if self._invalidation_bus is not None:
            from primer.int.coordinator import InvalidationTopic
            await self._invalidation_bus.publish(
                InvalidationTopic.TOOLSET, toolset_id,
            )
        else:
            await self._invalidate_toolset_local(toolset_id)

    # ---- Bus wiring -------------------------------------------------------

    async def bind_rate_limiter(self, rate_limiter: RateLimiter) -> None:
        """Wire the registry's adapter construction to the rate limiter.
        Idempotent."""
        self._rate_limiter = rate_limiter
        self._llm_factory = _build_default_llm_factory(
            rate_limiter=rate_limiter, trace_llm_io=self._trace_llm_io,
        )
        self._embedder_factory = _build_default_embedder_factory(rate_limiter=rate_limiter)
        self._cross_encoder_factory = _build_default_cross_encoder_factory(rate_limiter=rate_limiter)

    async def bind_invalidation_bus(self, bus: InvalidationBus) -> None:
        """Wire the registry's cache eviction to the bus. Idempotent."""
        if self._invalidation_bus is not None:
            return
        from primer.int.coordinator import InvalidationTopic

        self._invalidation_bus = bus

        async def _llm(key: str) -> None:
            await self._invalidate_llm_local(key)

        async def _embed(key: str) -> None:
            await self._invalidate_embedder_local(key)

        async def _cross(key: str) -> None:
            await self._invalidate_cross_encoder_local(key)

        async def _tool(key: str) -> None:
            await self._invalidate_toolset_local(key)

        self._invalidation_subs.append(
            await bus.subscribe(InvalidationTopic.LLM_PROVIDER, _llm)
        )
        self._invalidation_subs.append(
            await bus.subscribe(InvalidationTopic.EMBEDDING_PROVIDER, _embed)
        )
        self._invalidation_subs.append(
            await bus.subscribe(InvalidationTopic.CROSS_ENCODER_PROVIDER, _cross)
        )
        self._invalidation_subs.append(
            await bus.subscribe(InvalidationTopic.TOOLSET, _tool)
        )

    # ---- Lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Close every cached adapter and clear all caches."""
        for sub in self._invalidation_subs:
            try:
                await sub.aclose()
            except Exception:  # noqa: BLE001 -- best-effort
                pass
        self._invalidation_subs.clear()
        async with self._lock:
            adapters: list[Any] = (
                list(self._llm_cache.values())
                + list(self._embedder_cache.values())
                + list(self._cross_encoder_cache.values())
                + list(self._toolset_cache.values())
            )
            self._llm_cache.clear()
            self._embedder_cache.clear()
            self._cross_encoder_cache.clear()
            self._toolset_cache.clear()
        for adapter in adapters:
            try:
                await adapter.aclose()
            except Exception as exc:  # noqa: BLE001 -- best-effort
                logger.warning(
                    "ProviderRegistry: aclose failed on %s: %s",
                    type(adapter).__name__,
                    exc,
                )


__all__ = [
    "ProviderRegistry",
    "RESERVED_CROSS_ENCODER_IDS",
    "RESERVED_EMBEDDER_IDS",
    "RESERVED_LLM_IDS",
    "RESERVED_SSP_IDS",
    "RESERVED_TOOLSET_IDS",
    "RESERVED_WORKSPACE_PROVIDER_IDS",
]
