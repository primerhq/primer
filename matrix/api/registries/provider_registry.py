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

from matrix.int.cross_encoder import CrossEncoder
from matrix.int.embedder import Embedder
from matrix.int.llm import LLM
from matrix.int.toolset import ToolsetProvider
from matrix.model.except_ import ConfigError, NotFoundError
from matrix.model.provider import (
    CrossEncoderProvider,
    CrossEncoderProviderType,
    EmbeddingProvider,
    EmbeddingProviderType,
    LLMProvider,
    LLMProviderType,
    Toolset,
)


if TYPE_CHECKING:
    from matrix.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


def _default_llm_factory(provider: LLMProvider) -> LLM:  # pragma: no cover
    """Dispatch on ``provider.provider`` to the right adapter constructor."""
    match provider.provider:
        case LLMProviderType.OPENRESPONSES:
            from matrix.llm.openresponses import OpenResponsesLLM
            return OpenResponsesLLM(provider)
        case LLMProviderType.ANTHROPIC:
            from matrix.llm.anthropic import AnthropicLLM
            return AnthropicLLM(provider)
        case LLMProviderType.GEMINI:
            from matrix.llm.gemini import GeminiLLM
            return GeminiLLM(provider)
        case LLMProviderType.OLLAMA:
            from matrix.llm.ollama import OllamaLLM
            return OllamaLLM(provider)
        case _:
            raise ConfigError(
                f"unknown LLM provider type {provider.provider!r}"
            )


def _default_embedder_factory(  # pragma: no cover
    provider: EmbeddingProvider,
) -> Embedder:
    match provider.provider:
        case EmbeddingProviderType.HUGGINGFACE:
            from matrix.embedder.huggingface import HuggingFaceEmbedder
            return HuggingFaceEmbedder(provider)
        case EmbeddingProviderType.OPENAI:
            from matrix.embedder.openai import OpenAIEmbedder
            return OpenAIEmbedder(provider)
        case EmbeddingProviderType.GEMINI:
            from matrix.embedder.gemini import GeminiEmbedder
            return GeminiEmbedder(provider)
        case _:
            raise ConfigError(
                f"unknown embedding provider type {provider.provider!r}"
            )


def _default_cross_encoder_factory(  # pragma: no cover
    provider: CrossEncoderProvider,
) -> CrossEncoder:
    match provider.provider:
        case CrossEncoderProviderType.HUGGINGFACE:
            from matrix.cross_encoder.huggingface import HuggingFaceCrossEncoder
            return HuggingFaceCrossEncoder(provider)
        case _:
            raise ConfigError(
                f"unknown cross-encoder provider type {provider.provider!r}"
            )


_SYSTEM_TOOLSET_ID = "_system"
_SEARCH_TOOLSET_ID = "_search"
_WORKSPACES_TOOLSET_ID = "_workspaces"
_MISC_TOOLSET_ID = "_misc"
# Reserved builtin id WITHOUT leading underscore (legacy — the `web`
# toolset shipped before the underscore-prefix convention was settled).
_WEB_TOOLSET_ID = "web"


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
        from matrix.model.provider import ToolsetProviderType

        if toolset.provider == ToolsetProviderType.MCP:
            from matrix.toolset.mcp import McpToolsetProvider
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
                "ids start with '_'."
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
    ) -> None:
        self._sp = storage_provider
        self._llm_factory = llm_factory or _default_llm_factory
        self._embedder_factory = embedder_factory or _default_embedder_factory
        self._cross_encoder_factory = (
            cross_encoder_factory or _default_cross_encoder_factory
        )
        self._toolset_factory = toolset_factory or _default_toolset_factory
        # Reserved id ``_system`` resolves to this immutable provider
        # without consulting storage. Set via app lifespan; tests
        # may leave it None and use the row-based path.
        self._system_toolset_provider = system_toolset_provider
        # Reserved id ``_search`` resolves to this provider when the
        # internal collections subsystem is active. Set lazily by the
        # subsystem bootstrap (or the lifespan handler if a config row
        # already exists at startup); ``None`` means the subsystem is
        # inactive and ``get_toolset('_search')`` raises NotFoundError.
        self._search_toolset_provider = search_toolset_provider
        # Reserved id ``_workspaces`` resolves to this immutable
        # provider — always built at app startup. Mirrors ``_system``:
        # its tools dogfood the workspace REST API to agents.
        self._workspaces_toolset_provider = workspaces_toolset_provider
        # Reserved id ``_misc`` resolves to this immutable provider —
        # always built at app startup. Stateless utilities
        # (get_datetime, sleep, uuid_v4, hash, calculate).
        self._misc_toolset_provider = misc_toolset_provider
        # Reserved id ``web`` (no underscore prefix — legacy) resolves
        # to the immutable web toolset built at app startup.
        # DuckDuckGo search + http-request primitives.
        self._web_toolset_provider = web_toolset_provider

        self._llm_cache: dict[str, LLM] = {}
        self._embedder_cache: dict[str, Embedder] = {}
        self._cross_encoder_cache: dict[str, CrossEncoder] = {}
        self._toolset_cache: dict[str, ToolsetProvider] = {}
        self._lock = asyncio.Lock()

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
        # Reserved id `_system` short-circuits storage. Returns the
        # singleton built at app startup; immutable, never re-created.
        if (
            self._system_toolset_provider is not None
            and toolset_id == _SYSTEM_TOOLSET_ID
        ):
            return self._system_toolset_provider
        # Reserved id `_search` resolves to the search toolset built
        # when the internal collections subsystem is activated.
        if (
            self._search_toolset_provider is not None
            and toolset_id == _SEARCH_TOOLSET_ID
        ):
            return self._search_toolset_provider
        # Reserved id `_workspaces` resolves to the always-on
        # workspace dogfood toolset built at app startup.
        if (
            self._workspaces_toolset_provider is not None
            and toolset_id == _WORKSPACES_TOOLSET_ID
        ):
            return self._workspaces_toolset_provider
        # Reserved id `_misc` resolves to the always-on misc utility
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

    # ---- Invalidation -----------------------------------------------------

    async def invalidate_llm(self, provider_id: str) -> None:
        async with self._lock:
            adapter = self._llm_cache.pop(provider_id, None)
        if adapter is not None:
            await adapter.aclose()

    async def invalidate_embedder(self, provider_id: str) -> None:
        async with self._lock:
            adapter = self._embedder_cache.pop(provider_id, None)
        if adapter is not None:
            await adapter.aclose()

    async def invalidate_cross_encoder(self, provider_id: str) -> None:
        async with self._lock:
            adapter = self._cross_encoder_cache.pop(provider_id, None)
        if adapter is not None:
            await adapter.aclose()

    async def invalidate_toolset(self, toolset_id: str) -> None:
        # The reserved internal toolsets are immutable; invalidation
        # is a no-op so the singletons survive any cascade triggered
        # by an accidental write to the reserved ids.
        if toolset_id in (
            _SYSTEM_TOOLSET_ID,
            _SEARCH_TOOLSET_ID,
            _WORKSPACES_TOOLSET_ID,
            _MISC_TOOLSET_ID,
            _WEB_TOOLSET_ID,
        ):
            return
        async with self._lock:
            adapter = self._toolset_cache.pop(toolset_id, None)
        if adapter is not None:
            await adapter.aclose()

    # ---- Lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Close every cached adapter and clear all caches."""
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


__all__ = ["ProviderRegistry"]
