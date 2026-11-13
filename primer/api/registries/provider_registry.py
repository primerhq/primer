"""Lazy, invalidatable adapter registry for provider rows.

Holds one adapter instance per ``(LLMProvider, EmbeddingProvider,
CrossEncoderProvider, Toolset)`` row id; constructs adapters on first
read; drops the cached adapter (after calling its ``aclose``) when the
underlying row is mutated or deleted.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable, Iterator, Mapping, Set
from typing import TYPE_CHECKING, Any

from primer.common.toolset_ids import canonical_toolset_id
from primer.api.registries.retirement import RETIRE_IDLE_SECONDS, AdapterRetirement
from primer.int.coordinator import InvalidationBus, InvalidationSubscription, RateLimiter
from primer.int.cross_encoder import CrossEncoder
from primer.int.embedder import Embedder
from primer.int.llm import LLM
from primer.int.toolset import ToolsetProvider
from primer.model.except_ import ConfigError, NotFoundError
from primer.model.model_profile import ModelProfile
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
    from primer.llm.aggregated import AggregatedLLM
    from primer.toolset.oauth.service import ToolsetOAuthService


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
    def _construct(provider: LLMProvider) -> LLM:  # pragma: no cover
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
            case LLMProviderType.OPENROUTER:
                from primer.llm.openrouter import OpenRouterLLM
                return OpenRouterLLM(
                    provider, rate_limiter=rate_limiter, trace_llm_io=trace_llm_io,
                )
            case _:
                raise ConfigError(
                    f"unknown LLM provider type {provider.provider!r}"
                )

    def _factory(provider: LLMProvider) -> LLM:
        adapter = _construct(provider)
        from primer.llm.retrying import RetryingLLM
        return RetryingLLM(adapter, provider)

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
                from primer.common.optional import require_extra

                require_extra("huggingface", "the HuggingFace embedder")
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
                from primer.common.optional import require_extra

                require_extra("huggingface", "the HuggingFace cross-encoder")
                from primer.cross_encoder.huggingface import (
                    HuggingFaceCrossEncoder,
                )
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
_WORKSPACES_TOOLSET_ID = "workspaces"
_MISC_TOOLSET_ID = "misc"
# `web` has always been prefix-less; unchanged.
_WEB_TOOLSET_ID = "web"
_HARNESS_TOOLSET_ID = "harness"
_TRIGGER_TOOLSET_ID = "trigger"
_WORKSPACE_EXT_TOOLSET_ID = "workspace_ext"
_COLLECTIONS_TOOLSET_ID = "collections"
_CRUD_TOOLSET_ID = "crud"
# The session canvas (pivot P5, design 8.3): canvas_get, canvas_apply and canvas_wait.
_CANVAS_TOOLSET_ID = "canvas"

# Public: ids that are always resolvable by the live registry (built-in
# providers), so external reference-integrity checks can skip the
# Toolset storage lookup for them.
_FIXED_RESERVED_TOOLSET_IDS: frozenset[str] = frozenset({
    _SYSTEM_TOOLSET_ID,
    _WORKSPACES_TOOLSET_ID,
    _MISC_TOOLSET_ID,
    _WEB_TOOLSET_ID,
    _HARNESS_TOOLSET_ID,
    _TRIGGER_TOOLSET_ID,
    _WORKSPACE_EXT_TOOLSET_ID,
    _COLLECTIONS_TOOLSET_ID,
    _CRUD_TOOLSET_ID,
    _CANVAS_TOOLSET_ID,
})

_GENERATED_API_TOOLSET_IDS: frozenset[str] = frozenset()


class _ReservedToolsetIds(Set[str]):
    """Stable public view of fixed plus generated in-process providers."""

    @classmethod
    def _from_iterable(cls, values):
        return frozenset(values)

    def __contains__(self, value: object) -> bool:
        return value in _FIXED_RESERVED_TOOLSET_IDS or value in _GENERATED_API_TOOLSET_IDS

    def __iter__(self) -> Iterator[str]:
        return iter(_FIXED_RESERVED_TOOLSET_IDS | _GENERATED_API_TOOLSET_IDS)

    def __len__(self) -> int:
        return len(_FIXED_RESERVED_TOOLSET_IDS | _GENERATED_API_TOOLSET_IDS)


RESERVED_TOOLSET_IDS: Set[str] = _ReservedToolsetIds()


def register_generated_api_toolset_ids(provider_ids: Set[str]) -> None:
    """Publish the generated-provider half of the one reserved-ID union."""

    global _GENERATED_API_TOOLSET_IDS
    generated = frozenset(provider_ids)
    collision = generated & _FIXED_RESERVED_TOOLSET_IDS
    if collision:
        raise ConfigError(
            f"generated API provider ids collide with fixed built-ins: {sorted(collision)}"
        )
    _GENERATED_API_TOOLSET_IDS = _GENERATED_API_TOOLSET_IDS | generated



async def toolsets_off_by_design(storage_provider: "StorageProvider") -> frozenset[str]:
    """The reserved toolsets this install has switched off on purpose, not through a failure.

    ``canvas`` while ``CanvasSettings.agent_tools_enabled`` is off (an operator choice now that the canvas panel ships and the toolset is on by default).
    ``get_toolset`` answers such an id as unknown; a reader that expects every reserved toolset (the boot wiki build, the seed, the MCP
    catalogue) leaves these out instead of reporting them missing. Raises what ``read_canvas_settings`` raises for an undecodable row.
    """
    from primer.canvas.settings import read_canvas_settings

    if (await read_canvas_settings(storage_provider)).agent_tools_enabled:
        return frozenset()
    return frozenset({_CANVAS_TOOLSET_ID})


# Toolset ids that are SCOPES of the tool manager, not built-in providers: ``external`` is the invoker-supplied per-invocation
# tool scope, ``workspace`` / ``workspace_ext`` the workspace-tool scopes, ``client`` the per-turn client tools (a grant scope the
# manager exempts from the agent's allowlist, and a direct scope of the tool catalog). None of them may exist as a stored Toolset row:
# a row under ``client`` was a second toolset behind the scope, listed past the allowlist and reached by call_tool while describe_tool
# documented the static client tool (audit G6-9).
RESERVED_TOOLSET_SCOPE_IDS: frozenset[str] = frozenset({"external", "workspace", "workspace_ext", "client"})

# Every id a STORED Toolset row may not take: the built-in providers above (get_toolset answers them before it reads storage, so a
# row under one is a ghost that GET /v1/toolsets lists and nothing ever runs) plus the tool-manager scopes. The toolset REST router
# and the system toolset's ``create_toolset`` both refuse exactly this set (one definition, so they cannot drift). The REST hook
# used to refuse only the scope ids, because a module-local alias rebound the name RESERVED_TOOLSET_IDS to them.
class _ReservedToolsetRowIds(Set[str]):
    @classmethod
    def _from_iterable(cls, values):
        return frozenset(values)

    def __contains__(self, value: object) -> bool:
        return value in RESERVED_TOOLSET_IDS or value in RESERVED_TOOLSET_SCOPE_IDS

    def __iter__(self) -> Iterator[str]:
        return iter(set(RESERVED_TOOLSET_IDS) | RESERVED_TOOLSET_SCOPE_IDS)

    def __len__(self) -> int:
        return len(set(RESERVED_TOOLSET_IDS) | RESERVED_TOOLSET_SCOPE_IDS)


RESERVED_TOOLSET_ROW_IDS: Set[str] = _ReservedToolsetRowIds()

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
RESERVED_SSP_IDS: frozenset[str] = frozenset({"lance", "pgvector"})

# Cross-encoder providers: id "huggingface-ce" is the local reranker.
RESERVED_CROSS_ENCODER_IDS: frozenset[str] = frozenset({"huggingface-ce"})

# LLM providers: no reserved ids — LLMs always require explicit operator
# provisioning (API keys); the set is defined here for symmetry so
# router guards can import a uniform name.
RESERVED_LLM_IDS: frozenset[str] = frozenset()

# Workspace providers: id "local" is the local-filesystem backend.
RESERVED_WORKSPACE_PROVIDER_IDS: frozenset[str] = frozenset({"local"})

# The retired `_*` toolset ids (`_system` for `system`, ...) and their mapping live in primer/common/toolset_ids.py, which the approval gate shares and which
# imports nothing; `get_toolset` below resolves an old id to its current one through `canonical_toolset_id`, re-exported here as it always was.


def _build_default_toolset_factory(
    *,
    allowed_stdio_commands: frozenset[str] | None = None,
    oauth: "ToolsetOAuthService | None" = None,
    storage_provider: Any = None,
) -> Callable[[Toolset], ToolsetProvider]:
    """Build the default ``toolset_factory`` closure.

    ``allowed_stdio_commands`` is forwarded to every constructed
    :class:`McpToolsetProvider` so stdio launches are restricted to the
    operator-supplied safelist. ``None`` disables the check (acceptable
    only when Toolset creation is operator-restricted upstream).

    With a ``storage_provider``, an http / sse MCP provider connects through the egress guard unless an admin authored the toolset
    (:func:`primer.toolset.mcp_egress.author_is_trusted`, read per dispatch): a user-authored toolset cannot reach internal addresses. Without one
    (test fallbacks) nothing is guarded.
    """
    def _factory(toolset: Toolset) -> ToolsetProvider:  # pragma: no cover
        from primer.model.provider import ToolsetProviderType

        if toolset.provider == ToolsetProviderType.MCP:
            from primer.toolset.mcp import McpToolsetProvider
            guard: Any = False
            if storage_provider is not None:
                from primer.toolset.mcp_egress import author_is_trusted

                async def guard() -> bool:  # noqa: F811 - the per-dispatch decision: guard unless an admin authored it
                    return not await author_is_trusted(storage_provider, toolset)

            return McpToolsetProvider(
                toolset_id=toolset.id,
                config=toolset.config,
                oauth=oauth,
                allowed_stdio_commands=allowed_stdio_commands,
                egress_guard=guard,
            )
        if toolset.provider == ToolsetProviderType.PYTHON:
            from primer.toolset.python_runner.provider import PythonToolsetProvider
            from primer.toolset.python_runner.runners import LocalHardenedRunner

            return PythonToolsetProvider(
                toolset_id=toolset.id,
                config=toolset.config,
                runner=LocalHardenedRunner(
                    env={
                        k: v.get_secret_value()
                        for k, v in (toolset.config.env or {}).items()
                    }
                ),
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
        workspaces_toolset_provider: ToolsetProvider | None = None,
        misc_toolset_provider: ToolsetProvider | None = None,
        collections_toolset_provider: ToolsetProvider | None = None,
        web_toolset_provider: ToolsetProvider | None = None,
        harness_toolset_provider: ToolsetProvider | None = None,
        trigger_toolset_provider: ToolsetProvider | None = None,
        workspace_ext_toolset_provider: ToolsetProvider | None = None,
        crud_toolset_provider: ToolsetProvider | None = None,
        canvas_toolset_provider: ToolsetProvider | None = None,
        generated_api_toolset_providers: Mapping[str, ToolsetProvider] | None = None,
        rate_limiter: RateLimiter | None = None,
        trace_llm_io: bool = False,
        retire_idle_seconds: float = RETIRE_IDLE_SECONDS,
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
        # One canonical table owns every immutable provider. Tests may leave
        # a fixed provider absent to exercise the historical storage fallback.
        fixed_toolsets = {
            _SYSTEM_TOOLSET_ID: system_toolset_provider,
            _WORKSPACES_TOOLSET_ID: workspaces_toolset_provider,
            _MISC_TOOLSET_ID: misc_toolset_provider,
            _COLLECTIONS_TOOLSET_ID: collections_toolset_provider,
            _WEB_TOOLSET_ID: web_toolset_provider,
            _HARNESS_TOOLSET_ID: harness_toolset_provider,
            _TRIGGER_TOOLSET_ID: trigger_toolset_provider,
            _WORKSPACE_EXT_TOOLSET_ID: workspace_ext_toolset_provider,
            _CRUD_TOOLSET_ID: crud_toolset_provider,
            _CANVAS_TOOLSET_ID: canvas_toolset_provider,
        }
        generated_toolsets = dict(generated_api_toolset_providers or {})
        collisions = set(fixed_toolsets) & set(generated_toolsets)
        if collisions:
            raise ConfigError(
                f"generated API providers collide with fixed built-ins: {sorted(collisions)}"
            )
        # One canonical mapping owns every immutable provider. Aliases are
        # canonicalised before this lookup; unavailable fixed providers remain
        # absent and preserve the existing storage-fallback test seam.
        self._builtin_toolset_providers: dict[str, ToolsetProvider] = {
            provider_id: provider
            for provider_id, provider in fixed_toolsets.items()
            if provider is not None
        }
        self._builtin_toolset_providers.update(generated_toolsets)
        # Provenance for the generated subset of the canonical mapping. A
        # test process may compile more than one app against one registry;
        # only entries installed through the generated-provider seam may be
        # replaced by the next app build.
        self._generated_api_toolset_ids = set(generated_toolsets)

        self._retirement = AdapterRetirement(retire_idle_seconds)
        self._llm_cache: dict[str, LLM] = {}
        self._embedder_cache: dict[str, Embedder] = {}
        self._cross_encoder_cache: dict[str, CrossEncoder] = {}
        self._toolset_cache: dict[str, ToolsetProvider] = {}
        # Keyed by ModelProfile id (kind="aggregated"), not LLMProvider id
        # -- aggregation moved to the model-profile layer, so this is a
        # SEPARATE cache from _llm_cache, not a reuse of it. Exists so
        # AggregatedLLM's round-robin _cursor persists across calls: a
        # fresh instance per resolve_llm() call (the alternative) would
        # reset the cursor every time, so RANDOM/ROUND_ROBIN routing would
        # never actually rotate past member[0]. See get_aggregated_llm.
        self._aggregated_llm_cache: dict[str, "AggregatedLLM"] = {}
        self._lock = asyncio.Lock()
        # Bumped by every cache flush. Each ``get_*`` samples it before the
        # storage await and refuses to cache the adapter it built if the
        # value moved while it was suspended -- that adapter was built from
        # a row read BEFORE the flush, so caching it would resurrect exactly
        # the staleness the flush exists to clear (see arch-review batch 1,
        # MEDIUM-2).
        self._cache_generation = 0
        # One lock per (cache, id) being built, held across that miss's storage read and build only: a hit takes no lock and a miss never
        # waits for another id's (GX-7). Weak values: a lock lives only while a lookup holds it.
        self._build_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = weakref.WeakValueDictionary()
        self._invalidation_bus: InvalidationBus | None = None
        self._invalidation_subs: list[InvalidationSubscription] = []

    def install_generated_api_toolsets(
        self, providers: Mapping[str, ToolsetProvider]
    ) -> None:
        """Replace one app's generated subset in the canonical mapping.

        Rebuilding a test app against the same registry replaces only the
        providers whose generated provenance this registry recorded. Fixed
        built-ins and unrelated occupants remain collision errors.
        """

        incoming = dict(providers)
        incoming_ids = set(incoming)
        fixed_collisions = incoming_ids & _FIXED_RESERVED_TOOLSET_IDS
        if fixed_collisions:
            raise ConfigError(
                "generated API providers collide with fixed built-ins: "
                f"{sorted(fixed_collisions)}"
            )
        foreign_collisions = (
            incoming_ids
            & set(self._builtin_toolset_providers)
            - self._generated_api_toolset_ids
        )
        if foreign_collisions:
            raise ConfigError(
                "generated API providers collide with unrelated installed "
                f"providers: {sorted(foreign_collisions)}"
            )

        for provider_id in self._generated_api_toolset_ids - incoming_ids:
            self._builtin_toolset_providers.pop(provider_id, None)
        self._builtin_toolset_providers.update(incoming)
        self._generated_api_toolset_ids = incoming_ids

    def builtin_toolset_providers(self) -> Mapping[str, ToolsetProvider]:
        """The fixed built-in and installed generated providers as one map (M5).

        Read by the curated-route liveness guard, which needs the providers
        AS INSTALLED: a toolset an operator switched off by design is still
        the live source of its tools, so the guard does not go through
        :meth:`get_toolset` (whose canvas off-switch raises ``NotFoundError``).
        """

        return dict(self._builtin_toolset_providers)

    def _set_fixed_toolset(self, provider_id: str, provider: ToolsetProvider | None) -> None:
        """Wire a late-built fixed provider into the canonical table."""

        self._generated_api_toolset_ids.discard(provider_id)
        if provider is None:
            self._builtin_toolset_providers.pop(provider_id, None)
        else:
            self._builtin_toolset_providers[provider_id] = provider

    # These private properties preserve the existing lifespan/test wiring
    # seams while keeping lookup ownership in one canonical mapping.
    @property
    def _system_toolset_provider(self) -> ToolsetProvider | None:
        return self._builtin_toolset_providers.get(_SYSTEM_TOOLSET_ID)

    @_system_toolset_provider.setter
    def _system_toolset_provider(self, provider: ToolsetProvider | None) -> None:
        self._set_fixed_toolset(_SYSTEM_TOOLSET_ID, provider)

    @property
    def _workspaces_toolset_provider(self) -> ToolsetProvider | None:
        return self._builtin_toolset_providers.get(_WORKSPACES_TOOLSET_ID)

    @_workspaces_toolset_provider.setter
    def _workspaces_toolset_provider(self, provider: ToolsetProvider | None) -> None:
        self._set_fixed_toolset(_WORKSPACES_TOOLSET_ID, provider)

    @property
    def _misc_toolset_provider(self) -> ToolsetProvider | None:
        return self._builtin_toolset_providers.get(_MISC_TOOLSET_ID)

    @_misc_toolset_provider.setter
    def _misc_toolset_provider(self, provider: ToolsetProvider | None) -> None:
        self._set_fixed_toolset(_MISC_TOOLSET_ID, provider)

    @property
    def _collections_toolset_provider(self) -> ToolsetProvider | None:
        return self._builtin_toolset_providers.get(_COLLECTIONS_TOOLSET_ID)

    @_collections_toolset_provider.setter
    def _collections_toolset_provider(self, provider: ToolsetProvider | None) -> None:
        self._set_fixed_toolset(_COLLECTIONS_TOOLSET_ID, provider)

    @property
    def _web_toolset_provider(self) -> ToolsetProvider | None:
        return self._builtin_toolset_providers.get(_WEB_TOOLSET_ID)

    @_web_toolset_provider.setter
    def _web_toolset_provider(self, provider: ToolsetProvider | None) -> None:
        self._set_fixed_toolset(_WEB_TOOLSET_ID, provider)

    @property
    def _harness_toolset_provider(self) -> ToolsetProvider | None:
        return self._builtin_toolset_providers.get(_HARNESS_TOOLSET_ID)

    @_harness_toolset_provider.setter
    def _harness_toolset_provider(self, provider: ToolsetProvider | None) -> None:
        self._set_fixed_toolset(_HARNESS_TOOLSET_ID, provider)

    @property
    def _trigger_toolset_provider(self) -> ToolsetProvider | None:
        return self._builtin_toolset_providers.get(_TRIGGER_TOOLSET_ID)

    @_trigger_toolset_provider.setter
    def _trigger_toolset_provider(self, provider: ToolsetProvider | None) -> None:
        self._set_fixed_toolset(_TRIGGER_TOOLSET_ID, provider)

    @property
    def _workspace_ext_toolset_provider(self) -> ToolsetProvider | None:
        return self._builtin_toolset_providers.get(_WORKSPACE_EXT_TOOLSET_ID)

    @_workspace_ext_toolset_provider.setter
    def _workspace_ext_toolset_provider(self, provider: ToolsetProvider | None) -> None:
        self._set_fixed_toolset(_WORKSPACE_EXT_TOOLSET_ID, provider)

    @property
    def _crud_toolset_provider(self) -> ToolsetProvider | None:
        return self._builtin_toolset_providers.get(_CRUD_TOOLSET_ID)

    @_crud_toolset_provider.setter
    def _crud_toolset_provider(self, provider: ToolsetProvider | None) -> None:
        self._set_fixed_toolset(_CRUD_TOOLSET_ID, provider)

    @property
    def _canvas_toolset_provider(self) -> ToolsetProvider | None:
        return self._builtin_toolset_providers.get(_CANVAS_TOOLSET_ID)

    @_canvas_toolset_provider.setter
    def _canvas_toolset_provider(self, provider: ToolsetProvider | None) -> None:
        self._set_fixed_toolset(_CANVAS_TOOLSET_ID, provider)

    # ---- Lookups ----------------------------------------------------------


    @property
    def storage_provider(self) -> "StorageProvider":
        """The storage the registry resolves rows from (read access checks on a toolset row use it)."""
        return self._sp

    async def _cached(
        self, kind: str, cache: dict[str, Any], key: str, build: Callable[[], Awaitable[Any]], *, retire_uncached: bool = False,
    ) -> Any:
        """``cache[key]``, else what ``build`` makes, cached unless an eviction or a flush landed while it was built (the generation moved): that
        adapter came from a row read before it, so it serves this caller only. ``retire_uncached`` (the kinds whose adapters are tracked for
        in-flight calls) retires it too, so its client is closed once it has been idle; a kind that is not tracked is never retired, as a retire would
        close it under a call that is still using it."""
        cached = cache.get(key)
        if cached is not None:
            return cached
        lock = self._build_locks.setdefault((kind, key), asyncio.Lock())
        async with lock:
            cached = cache.get(key)
            if cached is not None:
                return cached
            generation = self._cache_generation
            adapter = await build()
            if self._cache_generation == generation:
                cache[key] = adapter
            elif retire_uncached:
                self._retirement.retire(adapter, f"{kind} {key}")
            return adapter

    async def get_llm(self, provider_id: str) -> LLM:
        async def build() -> LLM:
            row = await self._sp.get_storage(LLMProvider).get(provider_id)
            if row is None:
                raise NotFoundError(
                    f"LLMProvider {provider_id!r} does not exist"
                )
            return self._retirement.track(self._llm_factory(row))

        return await self._cached("llm", self._llm_cache, provider_id, build, retire_uncached=True)

    async def get_aggregated_llm(
        self,
        profile_id: str,
        *,
        resolve_member: "Callable[[str], Any]",
    ) -> "AggregatedLLM":
        """Return the cached :class:`AggregatedLLM` for ``profile_id``.

        Keyed by ModelProfile id (``kind="aggregated"``) in a cache
        SEPARATE from :attr:`_llm_cache` -- aggregation lives at the
        model-profile layer, not the LLMProvider layer, so there is no
        provider row to key on. Exists so :attr:`AggregatedLLM._cursor`
        (round-robin rotation state) persists across calls: without this,
        a caller that builds a fresh ``AggregatedLLM`` on every
        ``resolve_llm()`` call would reset the cursor every time, and
        ROUND_ROBIN routing would never rotate past member[0].

        Takes an id and re-fetches the row itself, after sampling the
        cache generation, exactly mirroring :meth:`get_llm` -- an earlier version took the
        caller's (:func:`primer.model_profile.resolve_llm`) already-fetched
        row instead, on the reasoning that the caller's own fetch already
        told it which branch to take. That was a real bug, not a style
        choice: the caller's fetch happens OUTSIDE this lock, across a
        multi-await window (its own row fetch plus per-member fetches in
        ``_flatten``), so an invalidation landing in that window was
        invisible here -- the generation sample below had zero awaits
        between it and the cache insert, so it could never observe a
        flush that happened before this method was even entered. Caching
        is what introduces the race (a fresh-construction-per-call design
        could never go stale); re-fetching after the sample is what closes it, the
        same way it's already closed for every other cache in this class.
        One redundant read per cache-miss is negligible at
        once-per-turn-build call frequency.

        ``resolve_member`` is only consulted on a cache MISS: a cache hit
        reuses the instance (and its closure) built on the FIRST call for
        this profile id, matching :meth:`get_llm`'s own precedent of
        ignoring anything about the calling context beyond the id once
        cached.
        """
        async def build() -> "AggregatedLLM":
            row = await self._sp.get_storage(ModelProfile).get(profile_id)
            if row is None:
                raise NotFoundError(
                    f"ModelProfile {profile_id!r} does not exist"
                )
            from primer.llm.aggregated import AggregatedLLM
            return self._retirement.track(AggregatedLLM(row, resolve_member=resolve_member))

        return await self._cached("aggregated_llm", self._aggregated_llm_cache, profile_id, build, retire_uncached=True)

    async def get_embedder(self, provider_id: str) -> Embedder:
        async def build() -> Embedder:
            row = await self._sp.get_storage(EmbeddingProvider).get(provider_id)
            if row is None:
                raise NotFoundError(
                    f"EmbeddingProvider {provider_id!r} does not exist"
                )
            return self._retirement.track(self._embedder_factory(row))

        return await self._cached("embedder", self._embedder_cache, provider_id, build, retire_uncached=True)

    async def get_cross_encoder(self, provider_id: str) -> CrossEncoder:
        async def build() -> CrossEncoder:
            row = await self._sp.get_storage(CrossEncoderProvider).get(provider_id)
            if row is None:
                raise NotFoundError(
                    f"CrossEncoderProvider {provider_id!r} does not exist"
                )
            return self._retirement.track(self._cross_encoder_factory(row))

        return await self._cached("cross_encoder", self._cross_encoder_cache, provider_id, build, retire_uncached=True)

    async def toolsets_off_by_design(self) -> frozenset[str]:
        """The reserved toolsets this install has switched off on purpose (module :func:`toolsets_off_by_design`)."""
        return await toolsets_off_by_design(self._sp)

    async def get_toolset(self, toolset_id: str) -> ToolsetProvider:
        # Transparently resolve old `_*` ids to new prefix-less ids so
        # agent rows persisted before the rename continue to work.
        toolset_id = canonical_toolset_id(toolset_id)
        builtin = self._builtin_toolset_providers.get(toolset_id)
        if builtin is not None:
            # Canvas is the one immutable provider an operator may disable.
            # Do not make every fixed/generated lookup read CanvasSettings:
            # a broken optional setting must not take the whole catalogue out.
            if (
                toolset_id == _CANVAS_TOOLSET_ID
                and toolset_id in await self.toolsets_off_by_design()
            ):
                raise NotFoundError(
                    f"Toolset {toolset_id!r} does not exist"
                )
            return builtin
        async def build() -> ToolsetProvider:
            row = await self._sp.get_storage(Toolset).get(toolset_id)
            if row is None:
                raise NotFoundError(
                    f"Toolset {toolset_id!r} does not exist"
                )
            return self._toolset_factory(row)

        return await self._cached("toolset", self._toolset_cache, toolset_id, build)

    # ---- Invalidation (private local helpers) ----------------------------

    async def _invalidate_llm_local(self, provider_id: str) -> None:
        async with self._lock:
            self._cache_generation += 1           # a build in flight for it must not cache what it read before
            adapter = self._llm_cache.pop(provider_id, None)
        if adapter is not None:
            self._retirement.retire(adapter)     # closed once its in-flight calls are done (GX-1)

    async def _invalidate_aggregated_llm_local(self, profile_id: str) -> None:
        async with self._lock:
            self._cache_generation += 1           # a build in flight for it must not cache what it read before
            adapter = self._aggregated_llm_cache.pop(profile_id, None)
        if adapter is not None:
            self._retirement.retire(adapter)     # closed once its in-flight calls are done (GX-1)

    async def _invalidate_embedder_local(self, provider_id: str) -> None:
        async with self._lock:
            self._cache_generation += 1           # a build in flight for it must not cache what it read before
            adapter = self._embedder_cache.pop(provider_id, None)
        if adapter is not None:
            self._retirement.retire(adapter)     # closed once its in-flight calls are done (GX-1)

    async def _invalidate_cross_encoder_local(self, provider_id: str) -> None:
        async with self._lock:
            self._cache_generation += 1           # a build in flight for it must not cache what it read before
            adapter = self._cross_encoder_cache.pop(provider_id, None)
        if adapter is not None:
            self._retirement.retire(adapter)     # closed once its in-flight calls are done (GX-1)

    async def _invalidate_toolset_local(self, toolset_id: str) -> None:
        # Transparently resolve old `_*` ids to new prefix-less ids.
        toolset_id = canonical_toolset_id(toolset_id)
        # The reserved internal toolsets are immutable; invalidation
        # is a no-op so the singletons survive any cascade triggered
        # by an accidental write to the reserved ids.
        if toolset_id in RESERVED_TOOLSET_IDS:
            return
        async with self._lock:
            self._cache_generation += 1           # a build in flight for it must not cache what it read before
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

    async def invalidate_aggregated_llm(self, profile_id: str) -> None:
        if self._invalidation_bus is not None:
            from primer.int.coordinator import InvalidationTopic
            await self._invalidation_bus.publish(
                InvalidationTopic.AGGREGATED_MODEL_PROFILE, profile_id,
            )
        else:
            await self._invalidate_aggregated_llm_local(profile_id)

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

    def _flush_caches_local(self) -> None:
        """Drop every cached adapter WITHOUT closing it.

        Invoked (synchronously) when an invalidation subscription
        reconnects: a NOTIFY dropped during the LISTEN blip may have left
        a stale adapter (e.g. a rotated API key) cached, so treat all
        cached adapters as potentially stale and force reconstruction on
        the next ``get_*``. This mirrors the cache-clearing half of
        :meth:`aclose` (minus the ``aclose()`` calls).

        No lock is taken: the reconnect hook runs synchronously in the
        bus supervisor task, and clearing dicts has no ``await`` points,
        so no coroutine can observe a half-cleared state. We deliberately
        do NOT ``aclose()`` the dropped adapters -- one may still be in
        use by an in-flight request, and closing its transport out from
        under it would break that request; dropping the cache entry alone
        is enough to force a fresh build next time.

        Because no lock is taken, this CAN land while a ``get_*`` is
        suspended at its storage await (``get_*`` holds only its own id's
        build lock across that await). That get
        would then re-populate the cache with an adapter built from a
        pre-flush row, leaving the stale adapter cached until restart --
        the exact bug the flush exists to fix. Bumping the generation
        makes any such in-flight get skip its cache insert (it still
        returns its adapter to its caller, whose request completes
        normally); the next get rebuilds from a post-flush row.
        """
        self._cache_generation += 1
        # The flushed adapters are retired, not dropped: an in-flight request keeps working on its own, and the adapter is closed once it is idle.
        for cache in (self._llm_cache, self._aggregated_llm_cache, self._embedder_cache, self._cross_encoder_cache):
            for adapter in cache.values():
                self._retirement.retire(adapter)
            cache.clear()
        self._toolset_cache.clear()

    async def bind_invalidation_bus(self, bus: InvalidationBus) -> None:
        """Wire the registry's cache eviction to the bus. Idempotent."""
        if self._invalidation_bus is not None:
            return
        from primer.int.coordinator import InvalidationTopic

        self._invalidation_bus = bus

        async def _llm(key: str) -> None:
            await self._invalidate_llm_local(key)

        async def _agg_llm(key: str) -> None:
            await self._invalidate_aggregated_llm_local(key)

        async def _embed(key: str) -> None:
            await self._invalidate_embedder_local(key)

        async def _cross(key: str) -> None:
            await self._invalidate_cross_encoder_local(key)

        async def _tool(key: str) -> None:
            await self._invalidate_toolset_local(key)

        # Every topic shares one broadcast channel, so a reconnect on any
        # subscription means invalidations on ALL topics may have been
        # dropped during the blip. Flush every cache on any reconnect --
        # over-invalidation (an extra rebuild) is safe; a stale adapter
        # (e.g. a rotated API key) cached until restart is not.
        self._invalidation_subs.append(
            await bus.subscribe(
                InvalidationTopic.LLM_PROVIDER, _llm,
                on_reconnect=self._flush_caches_local,
            )
        )
        self._invalidation_subs.append(
            await bus.subscribe(
                InvalidationTopic.AGGREGATED_MODEL_PROFILE, _agg_llm,
                on_reconnect=self._flush_caches_local,
            )
        )
        self._invalidation_subs.append(
            await bus.subscribe(
                InvalidationTopic.EMBEDDING_PROVIDER, _embed,
                on_reconnect=self._flush_caches_local,
            )
        )
        self._invalidation_subs.append(
            await bus.subscribe(
                InvalidationTopic.CROSS_ENCODER_PROVIDER, _cross,
                on_reconnect=self._flush_caches_local,
            )
        )
        self._invalidation_subs.append(
            await bus.subscribe(
                InvalidationTopic.TOOLSET, _tool,
                on_reconnect=self._flush_caches_local,
            )
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
                + list(self._aggregated_llm_cache.values())
                + list(self._embedder_cache.values())
                + list(self._cross_encoder_cache.values())
                + list(self._toolset_cache.values())
            )
            self._llm_cache.clear()
            self._aggregated_llm_cache.clear()
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
        await self._retirement.close_all()


__all__ = [
    "ProviderRegistry",
    "RESERVED_CROSS_ENCODER_IDS",
    "RESERVED_EMBEDDER_IDS",
    "RESERVED_LLM_IDS",
    "RESERVED_SSP_IDS",
    "RESERVED_TOOLSET_IDS",
    "RESERVED_TOOLSET_ROW_IDS",
    "RESERVED_TOOLSET_SCOPE_IDS",
    "RESERVED_WORKSPACE_PROVIDER_IDS",
    "canonical_toolset_id",
    "register_generated_api_toolset_ids",
    "toolsets_off_by_design",
]
