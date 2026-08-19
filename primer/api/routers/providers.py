"""Provider entity routers: LLM / Embedding / CrossEncoder / Toolset.

Each entity follows the standard CRUD + Find shape from
:mod:`primer.api.routers._crud`, plus entity-specific operations:

* LLMProvider:           ``GET /v1/llm_providers/{id}/models``
                         ``GET /v1/llm_providers/{id}/discovered_models``
                         ``POST /v1/llm_providers/{id}/invalidate``
                         ``POST /v1/llm_providers/_discover_models``
* EmbeddingProvider:     ``GET /v1/embedding_providers/{id}/models``
                         ``POST /v1/embedding_providers/{id}/invalidate``
                         ``POST /v1/embedding_providers/_discover_models``
* CrossEncoderProvider:  ``GET /v1/cross_encoder_providers/{id}/models``
                         ``POST /v1/cross_encoder_providers/{id}/invalidate``
* Toolset:               ``GET  /v1/toolsets/{id}/tools``
                         ``POST /v1/toolsets/{id}/invalidate``

PUT and DELETE on every entity cascade-invalidate the matching cached
adapter in the per-request ProviderRegistry via the CRUD callbacks.

The ``_discover_models`` endpoints accept a draft provider config
(provider type + config block) and live-probe the upstream API to
return the list of available models — used by the console's "Fetch
Models" button. They build a transient adapter, call its
``list_models()``, then dispose. They do NOT persist anything.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Request
from pydantic import BaseModel, Field, ValidationError

from primer.llm.anthropic import _discover_anthropic_models
from primer.llm.gemini import _discover_gemini_models
from primer.llm.openrouter import _discover_openrouter_models

from primer.api.deps import (
    get_cross_encoder_provider_storage,
    get_embedding_provider_storage,
    get_llm_provider_storage,
    get_model_profile_storage,
    get_principal,
    get_provider_registry,
    get_storage_provider,
    get_toolset_storage,
)
from primer.api.errors import common_responses
from primer.model.except_ import (
    BadRequestError,
    NetworkError,
    PrimerError,
    ToolsetUnreachableError,
)
from primer.api.registries import ProviderRegistry
from primer.api.registries.provider_registry import (
    RESERVED_CROSS_ENCODER_IDS,
    RESERVED_EMBEDDER_IDS,
    RESERVED_LLM_IDS,
)
from primer.api.routers._cdc_hooks import register_cdc_kind
from primer.api.routers._crud import make_crud_router
from primer.model.provider import (
    AnthropicConfig,
    CrossEncoderProvider,
    CrossEncoderProviderType,
    EmbeddingProvider,
    EmbeddingProviderType,
    GoogleConfig,
    LLMProvider,
    LLMProviderType,
    OpenAIEmbeddingFlavor,
    OpenChatFlavor,
    OpenResponsesFlavor,
    OpenRouterConfig,
    Toolset,
    ToolsetProviderType,
    TransportType,
)
from primer.api.routers._references import ReferenceCheck
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalPolicy


logger = logging.getLogger(__name__)


# ---- Reserved-id protection helpers ---------------------------------------


# Toolset ids claimed by the runtime's pseudo-toolsets. ``external`` is
# the invoker-supplied per-invocation tool scope; ``workspace`` /
# ``workspace_ext`` are the workspace-tool scopes composed by the tool
# manager. None of them may exist as stored Toolset rows.
RESERVED_TOOLSET_IDS = frozenset({"external", "workspace", "workspace_ext"})


def _make_reserved_create_guard(reserved_ids: frozenset[str], kind: str):
    """Return an ``on_pre_create`` hook that rejects POST with a reserved id."""
    async def _guard(entity, request: Request) -> None:
        if entity.id in reserved_ids:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "reserved_id",
                    "kind": kind,
                    "reserved": sorted(reserved_ids),
                    "message": (
                        f"id {entity.id!r} is reserved and cannot be "
                        "created via the API"
                    ),
                },
            )
    _guard.__name__ = f"_reject_reserved_{kind}_create"
    return _guard


def _make_reserved_delete_guard(reserved_ids: frozenset[str], kind: str):
    """Return an ``on_pre_delete_id`` hook that rejects DELETE of a reserved id."""
    async def _guard(entity_id: str, request: Request) -> None:
        if entity_id in reserved_ids:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "reserved_id_protected",
                    "kind": kind,
                    "message": (
                        f"id {entity_id!r} is a reserved {kind} and "
                        "cannot be deleted"
                    ),
                },
            )
    _guard.__name__ = f"_reject_reserved_{kind}_delete"
    return _guard


# Default seeded when a discovery probe cannot reveal a model's true
# context window (the OpenAI /v1/models and Ollama /api/tags endpoints
# do not include it). The form lets operators override per-model.
_DEFAULT_LLM_CONTEXT_LENGTH = 32000


# ---- Discovery body shapes -------------------------------------------------


class _DiscoverModelsBody(BaseModel):
    """Body for ``POST /v1/<entity>/_discover_models``.

    A draft provider config — same shape as a real provider entry but
    without ``id``, ``models``, or ``limits`` (the endpoint synthesizes
    those to satisfy the model validator before constructing the
    transient adapter).
    """

    provider: str = Field(..., description="Provider type discriminator.")
    config: dict[str, Any] = Field(
        ..., description="Provider-specific connection config.",
    )


def _build_stub_provider(
    model_cls: type,
    *,
    provider: str,
    config: dict[str, Any],
    models: list[dict[str, Any]],
) -> Any:
    """Construct a transient provider row for discovery probes.

    Validates the draft via the canonical Pydantic model so config-
    shape errors surface as a 400 with the field path, instead of an
    obscure 500 from inside the adapter constructor. The id and limits
    are synthesized — neither matters for ``list_models()``.
    """
    try:
        return model_cls.model_validate({
            "id": f"_probe_{uuid4().hex[:8]}",
            "provider": provider,
            "config": config,
            "models": models,
            "limits": {"max_concurrency": 1},
        })
    except ValidationError as e:
        raise BadRequestError(
            "Draft provider failed validation: " + str(e),
        ) from e


# ---- cascade-invalidation hooks --------------------------------------------


def _make_invalidator(method_name: str):
    """Build a CRUD-router hook that invalidates the named cache slot
    on the request's :class:`ProviderRegistry`."""
    async def _hook(entity_id: str, request: Request) -> None:
        registry: ProviderRegistry = request.app.state.provider_registry
        await getattr(registry, method_name)(entity_id)
    _hook.__name__ = f"_invalidate_{method_name.removeprefix('invalidate_')}"
    return _hook


_invalidate_llm = _make_invalidator("invalidate_llm")
_invalidate_embedder = _make_invalidator("invalidate_embedder")
_invalidate_cross_encoder = _make_invalidator("invalidate_cross_encoder")
_invalidate_toolset = _make_invalidator("invalidate_toolset")


# ---- LLMProvider router ----------------------------------------------------

# ---------- _types: form metadata for the console's provider form ---------
#
# The console used to carry this table itself (ui/components/providers.jsx
# PROVIDER_KINDS_FIELDS: "single source of truth mirroring the backend's
# provider enums + per-provider Config models ... Keep these in sync when
# the backend grows a new provider type"). Keeping them in sync by hand
# failed: the table offered openresponses flavors ["openai", "lmstudio",
# "other"] while OpenResponsesFlavor has carried VLLM all along. The enums
# live here, so the field shape is served from here.
#
# Shape follows the web-search helper (routers/web_search.py:209-222):
#   {provider_type: {label, config_fields, row_fields, discoverable}}
# The field lists carry descriptor dicts rather than bare names; the
# console normalises a bare string to {"key": <name>}, so the older
# string-list helpers stay valid unchanged.
#
# These routers MUST be mounted before their CRUD siblings in
# _app_routes.py, or GET /{provider_id} swallows "_types".


def _form_field(
    key: str,
    label: str,
    type_: str,
    *,
    required: bool = False,
    help_: str = "",
    options: list[str] | None = None,
    placeholder: str = "",
) -> dict[str, Any]:
    """One form-field descriptor for the console's parameterized form."""
    field: dict[str, Any] = {
        "key": key,
        "label": label,
        "type": type_,
        "required": required,
    }
    if help_:
        field["help"] = help_
    if options is not None:
        field["options"] = options
    if placeholder:
        field["placeholder"] = placeholder
    return field


def _base_url_field() -> dict[str, Any]:
    return _form_field(
        "url",
        "Base URL",
        "url",
        required=True,
        placeholder="https://api.openai.com/v1",
    )


def _optional_api_key_field() -> dict[str, Any]:
    return _form_field(
        "api_key",
        "API key (optional)",
        "password",
        help_=(
            "Leave blank for unauthenticated endpoints (LM Studio, "
            "self-hosted vLLM); the upstream returns 401 at call time if "
            "it does require auth."
        ),
    )


def _vendor_api_key_field(vendor: str) -> dict[str, Any]:
    return _form_field(
        "api_key",
        "API key (optional)",
        "password",
        help_=(
            f"Required for the real {vendor} API; leave blank only when an "
            "upstream proxy supplies auth."
        ),
    )


def _models_row_field(example: str) -> dict[str, Any]:
    return _form_field(
        "models",
        "Models",
        "model_list",
        required=True,
        help_=(
            f"At least one model name (for example {example}); the row is "
            "rejected without one."
        ),
    )


def _with_limits(types: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Mark every type as carrying a Limits block.

    ``limits`` is required on every model-family provider row and
    ``max_concurrency`` inside it has no default
    (primer/model/providers/_shared.py:44-49), so a form that omits it
    can only ever produce a 422. Flagged once here rather than repeated
    on every entry.
    """
    for meta in types.values():
        meta["limits"] = True
    return types


llm_provider_types_router = APIRouter(tags=["providers"])
embedding_provider_types_router = APIRouter(tags=["providers"])
cross_encoder_provider_types_router = APIRouter(tags=["providers"])


@llm_provider_types_router.get(
    "/llm_providers/_types",
    summary=(
        "Provider-type metadata for the console form: the field shape an "
        "operator fills in per LLM provider type."
    ),
)
async def list_llm_provider_types() -> dict[str, dict[str, Any]]:
    return _with_limits({
        LLMProviderType.OPENRESPONSES.value: {
            "label": "OpenAI Responses API (openresponses)",
            "config_fields": [
                _base_url_field(),
                _optional_api_key_field(),
                _form_field(
                    "flavor",
                    "Flavor",
                    "enum",
                    options=[f.value for f in OpenResponsesFlavor],
                ),
            ],
            "row_fields": [],
            "discoverable": True,
        },
        LLMProviderType.OPENCHAT.value: {
            "label": "OpenAI-compatible Chat Completions (openchat)",
            "config_fields": [
                _base_url_field(),
                _optional_api_key_field(),
                _form_field(
                    "flavor",
                    "Flavor",
                    "enum",
                    options=[f.value for f in OpenChatFlavor],
                ),
            ],
            "row_fields": [],
            "discoverable": True,
        },
        LLMProviderType.GEMINI.value: {
            "label": "Google Gemini",
            "config_fields": [_vendor_api_key_field("Gemini")],
            "row_fields": [],
            "discoverable": True,
        },
        LLMProviderType.ANTHROPIC.value: {
            "label": "Anthropic",
            "config_fields": [_vendor_api_key_field("Anthropic")],
            "row_fields": [],
            "discoverable": False,
        },
        LLMProviderType.OLLAMA.value: {
            "label": "Ollama",
            "config_fields": [
                _form_field(
                    "url",
                    "Base URL",
                    "url",
                    required=True,
                    placeholder="http://localhost:11434",
                ),
                _optional_api_key_field(),
            ],
            "row_fields": [],
            "discoverable": True,
        },
        LLMProviderType.OPENROUTER.value: {
            "label": "OpenRouter",
            "config_fields": [
                _form_field(
                    "api_key",
                    "API key",
                    "password",
                    required=True,
                    help_=(
                        "Required: the OpenRouter upstream is always remote "
                        "and always authenticated."
                    ),
                ),
                _form_field(
                    "app_name",
                    "App name (optional)",
                    "text",
                    placeholder="primer-staging",
                    help_="Sent as X-Title for OpenRouter app attribution.",
                ),
                _form_field(
                    "app_url",
                    "App URL (optional)",
                    "url",
                    placeholder="https://primer.example",
                    help_="Sent as HTTP-Referer for OpenRouter attribution.",
                ),
            ],
            "row_fields": [],
            "discoverable": True,
        },
        LLMProviderType.AGGREGATED.value: {
            "label": "Aggregated",
            # An ordered member list with routing and failover switches is
            # not a flat field set; the console mounts its own editor.
            "variant": "aggregated",
            "config_fields": [],
            "row_fields": [],
            "discoverable": False,
        },
    })


@embedding_provider_types_router.get(
    "/embedding_providers/_types",
    summary="Provider-type metadata for the embedding provider form.",
)
async def list_embedding_provider_types() -> dict[str, dict[str, Any]]:
    return _with_limits({
        EmbeddingProviderType.OPENAI.value: {
            "label": "OpenAI / OpenAI-compatible",
            "config_fields": [
                _base_url_field(),
                _optional_api_key_field(),
                _form_field(
                    "flavor",
                    "Flavor",
                    "enum",
                    options=[f.value for f in OpenAIEmbeddingFlavor],
                ),
            ],
            "row_fields": [_models_row_field("text-embedding-3-small")],
            "discoverable": True,
        },
        EmbeddingProviderType.HUGGINGFACE.value: {
            "label": "HuggingFace (local sentence-transformers)",
            "config_fields": [
                _form_field(
                    "token",
                    "HF token",
                    "password",
                    required=True,
                    help_=(
                        "Required to pull the transformer model, even for "
                        "public models."
                    ),
                ),
            ],
            "row_fields": [_models_row_field("BAAI/bge-base-en-v1.5")],
            "discoverable": False,
        },
        EmbeddingProviderType.GEMINI.value: {
            "label": "Google Gemini",
            "config_fields": [_vendor_api_key_field("Gemini")],
            "row_fields": [_models_row_field("gemini-embedding-001")],
            "discoverable": False,
        },
    })


@cross_encoder_provider_types_router.get(
    "/cross_encoder_providers/_types",
    summary="Provider-type metadata for the cross-encoder provider form.",
)
async def list_cross_encoder_provider_types() -> dict[str, dict[str, Any]]:
    return _with_limits({
        CrossEncoderProviderType.HUGGINGFACE.value: {
            "label": "HuggingFace (local sentence-transformers)",
            "config_fields": [
                _form_field(
                    "token",
                    "HF token (optional for public repos)",
                    "password",
                    help_=(
                        "Needed only for gated repositories; BAAI and "
                        "cross-encoder/* rerankers are public."
                    ),
                ),
            ],
            "row_fields": [_models_row_field("BAAI/bge-reranker-v2-m3")],
            "discoverable": False,
        },
    })


llm_provider_router = make_crud_router(
    model_cls=LLMProvider,
    storage_dep=get_llm_provider_storage,
    plural="llm_providers",
    tag="llm-providers",
    on_update=_invalidate_llm,
    on_delete=_invalidate_llm,
    on_pre_create=_make_reserved_create_guard(RESERVED_LLM_IDS, "llm_provider"),
    on_pre_delete_id=_make_reserved_delete_guard(RESERVED_LLM_IDS, "llm_provider"),
)


@llm_provider_router.post(
    "/llm_providers/{provider_id}/invalidate",
    status_code=204,
    summary="Invalidate cached LLM adapter",
    responses=common_responses(500),
)
async def invalidate_llm_provider(
    provider_id: str = Path(..., description="LLMProvider id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> None:
    await registry.invalidate_llm(provider_id)


@llm_provider_router.get(
    "/llm_providers/{provider_id}/models",
    summary="List the model names registered on this provider",
    responses=common_responses(404, 500),
)
async def get_llm_provider_models(
    provider_id: str = Path(..., description="LLMProvider id"),
    profiles=Depends(get_model_profile_storage),
    providers=Depends(get_llm_provider_storage),
) -> dict:
    """Return the distinct model names this provider can serve.

    Derived from the provider's :class:`ModelProfile` rows rather than the
    adapter: profiles replaced ``LLMProvider.models[]`` as the registry of
    what a provider serves, so this is a storage query and no longer needs
    to construct an adapter. Several profiles may share a model name (that
    is the point of profiles), so names are deduplicated.

    For a LIVE probe of what the upstream actually offers, use
    ``POST /v1/llm_providers/_discover_models`` -- that is what the
    console's Fetch action drives.
    """
    from primer.storage.q import Q
    from primer.model.except_ import NotFoundError
    from primer.model.model_profile import ModelProfile
    from primer.model.storage import OffsetPage

    # A provider with no profiles yet and a provider that does not exist
    # are different answers ([] vs 404), so check the row explicitly
    # rather than inferring absence from an empty profile list.
    if await providers.get(provider_id) is None:
        raise NotFoundError(f"LLMProvider {provider_id!r} does not exist")

    names: list[str] = []
    offset = 0
    while True:
        page = await profiles.find(
            Q(ModelProfile).where("provider_id", provider_id).build(),
            OffsetPage(offset=offset, length=200),
        )
        names.extend(p.model_name for p in page.items)
        if len(page.items) < 200:
            break
        offset += 200
    return {"models": sorted(set(names))}


@llm_provider_router.post(
    "/llm_providers/_discover_models",
    summary="Probe a draft LLM provider for its model list",
    responses=common_responses(400, 422, 502),
)
async def discover_llm_models(
    body: _DiscoverModelsBody = Body(...),
) -> dict:
    """Live-probe the upstream provider for its available models.

    Used by the console's "Fetch Models" button before any provider
    has been persisted. Returns ``{"models": [{name, context_length?}, ...]}``.

    For a provider that IS already persisted, use
    ``GET /v1/llm_providers/{id}/discovered_models`` instead: secrets are
    redacted on the wire, so a config round-tripped through a GET cannot
    authenticate a probe.

    ``ollama``, ``openresponses``, ``openchat``, ``openrouter``,
    ``anthropic``, and ``gemini`` expose a live list-models API. Any
    other provider type returns 400 and the frontend falls back to its
    curated suggested-model list.
    """
    return await _probe_llm_models(body.provider, body.config)


@llm_provider_router.get(
    "/llm_providers/{provider_id}/discovered_models",
    summary="Live-probe a saved LLM provider for its upstream model list",
    responses=common_responses(400, 404, 502),
)
async def discover_saved_llm_models(
    provider_id: str = Path(..., description="LLMProvider id"),
    providers=Depends(get_llm_provider_storage),
) -> dict:
    """Live-probe a provider that already exists, using its stored config.

    Distinct from ``GET /llm_providers/{id}/models``, which reports what is
    REGISTERED (its ModelProfile rows). This reports what the upstream
    currently OFFERS; the difference is what the console's Fetch action
    turns into new profiles.

    The draft-config variant above cannot serve the provider detail page:
    the row the console holds has its secrets redacted, so replaying that
    config would probe with ``"**********"`` as the API key. Reading the
    stored row server-side keeps the real secret where it belongs.

    Returns the same ``{"models": [...]}`` shape, which the console turns
    into :class:`ModelProfile` rows -- discovery lists what the upstream
    offers; a profile is what this deployment chooses to register.
    """
    from primer.model.except_ import NotFoundError

    row = await providers.get(provider_id)
    if row is None:
        raise NotFoundError(f"LLMProvider {provider_id!r} does not exist")
    # mode="json" would re-mask every SecretStr to "**********" -- the exact
    # value this endpoint exists to avoid sending upstream. Dump in python
    # mode and unwrap explicitly.
    config = _unwrap_secrets(row.config.model_dump(mode="python"))
    return await _probe_llm_models(str(getattr(row.provider, "value", row.provider)), config)


def _unwrap_secrets(value: Any) -> Any:
    """Recursively replace ``SecretStr`` with its real value.

    The probe helpers interpolate config values straight into headers, so a
    ``SecretStr`` that survives this would be sent literally as
    ``Bearer **********`` -- an auth failure that looks like a bad key.
    """
    from pydantic import SecretStr

    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {k: _unwrap_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_secrets(v) for v in value]
    return value


async def _probe_llm_models(provider: str, config: dict[str, Any]) -> dict:
    """Dispatch a live list-models probe by provider type.

    Note: the adapters' ``list_models()`` method is not used here (anomaly
    T0025); this bypasses the adapter and calls the provider's native list
    endpoint directly.
    """

    # Validate the draft via the canonical model so config shape errors
    # surface cleanly. We never persist or run anything from the stub.
    _build_stub_provider(
        LLMProvider,
        provider=provider,
        config=config,
        models=[{"name": "_probe", "context_length": 1}],
    )
    if provider == "ollama":
        result = await _probe_ollama_models(config)
    elif provider == "openresponses":
        result = await _probe_openai_compatible_models(config)
    elif provider == "openchat":
        # OpenAI-compatible Chat Completions: same /v1/models probe as
        # openresponses (both carry an OpenAI-style base URL).
        result = await _probe_openai_compatible_models(config)
    elif provider == "openrouter":
        try:
            draft = OpenRouterConfig.model_validate(config)
        except ValidationError as exc:
            raise BadRequestError(
                f"invalid OpenRouter config: {exc}",
            ) from exc
        try:
            catalogue = await _discover_openrouter_models(draft)
        except httpx.HTTPStatusError as exc:
            raise BadRequestError(
                f"OpenRouter discover failed: HTTP {exc.response.status_code} "
                f"{exc.response.text[:200]}",
            ) from exc
        except httpx.RequestError as exc:
            # Connect / timeout / read errors that are not HTTP responses.
            raise BadRequestError(
                f"OpenRouter discover network error: {type(exc).__name__}: "
                f"{exc}",
            ) from exc
        result = {"models": catalogue}
    elif provider == "anthropic":
        try:
            ant_draft = AnthropicConfig.model_validate(config)
        except ValidationError as exc:
            raise BadRequestError(
                f"invalid Anthropic config: {exc}",
            ) from exc
        try:
            catalogue = await _discover_anthropic_models(ant_draft)
        except httpx.HTTPStatusError as exc:
            raise BadRequestError(
                f"Anthropic discover failed: HTTP {exc.response.status_code} "
                f"{exc.response.text[:200]}",
            ) from exc
        except httpx.RequestError as exc:
            # Connect / timeout / read errors that are not HTTP responses.
            raise BadRequestError(
                f"Anthropic discover network error: {type(exc).__name__}: "
                f"{exc}",
            ) from exc
        result = {"models": catalogue}
    elif provider == "gemini":
        try:
            gem_draft = GoogleConfig.model_validate(config)
        except ValidationError as exc:
            raise BadRequestError(
                f"invalid Gemini config: {exc}",
            ) from exc
        try:
            catalogue = await _discover_gemini_models(gem_draft)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise BadRequestError(
                    "Gemini API key invalid or unauthorized (HTTP "
                    f"{status}); check the key from Google AI Studio.",
                ) from exc
            raise BadRequestError(
                f"Gemini discover failed: HTTP {status} "
                f"{exc.response.text[:200]}",
            ) from exc
        except httpx.RequestError as exc:
            # Connect / timeout / read errors that are not HTTP responses.
            raise BadRequestError(
                f"Gemini discover network error: {type(exc).__name__}: "
                f"{exc}",
            ) from exc
        result = {"models": catalogue}
    else:
        raise BadRequestError(
            f"live model discovery is not supported for provider "
            f"{provider!r}; populate the models list manually or "
            f"use the UI's 'Suggest models' fallback.",
        )
    # Neither Ollama's /api/tags, OpenAI's /v1/models, nor Anthropic's
    # /v1/models exposes a per-model context window. LLMModel requires
    # context_length, so seed a sane default the operator can override
    # in the form. OpenRouter's catalogue carries context_length
    # verbatim, so skip the default for that branch. Gemini reports
    # inputTokenLimit for most models but not all, so seed the default
    # only where the helper omitted it.
    if provider in ("ollama", "openresponses", "openchat", "anthropic", "gemini"):
        for m in result.get("models", []):
            m.setdefault("context_length", _DEFAULT_LLM_CONTEXT_LENGTH)
    return result


# ---- EmbeddingProvider router ----------------------------------------------

embedding_provider_router = make_crud_router(
    model_cls=EmbeddingProvider,
    storage_dep=get_embedding_provider_storage,
    plural="embedding_providers",
    tag="embedding-providers",
    on_update=_invalidate_embedder,
    on_delete=_invalidate_embedder,
    on_pre_create=_make_reserved_create_guard(RESERVED_EMBEDDER_IDS, "embedding_provider"),
    on_pre_delete_id=_make_reserved_delete_guard(RESERVED_EMBEDDER_IDS, "embedding_provider"),
)


@embedding_provider_router.post(
    "/embedding_providers/{provider_id}/invalidate",
    status_code=204,
    summary="Invalidate cached embedder adapter",
    responses=common_responses(500),
)
async def invalidate_embedding_provider(
    provider_id: str = Path(..., description="EmbeddingProvider id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> None:
    await registry.invalidate_embedder(provider_id)


@embedding_provider_router.get(
    "/embedding_providers/{provider_id}/models",
    summary="Fetch live model list from the embedding provider",
    responses=common_responses(404, 500, 502, 504),
)
async def get_embedding_provider_models(
    provider_id: str = Path(..., description="EmbeddingProvider id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> dict:
    adapter = await registry.get_embedder(provider_id)
    models = await adapter.list_models()
    return {"models": list(models)}


@embedding_provider_router.post(
    "/embedding_providers/_discover_models",
    summary="Probe a draft embedding provider for its model list",
    responses=common_responses(400, 422, 502),
)
async def discover_embedding_models(
    body: _DiscoverModelsBody = Body(...),
) -> dict:
    """Mirror of ``discover_llm_models`` for embedding providers.

    Only ``openai`` (OpenAI-compatible HTTP) is live-discoverable.
    ``huggingface`` (local sentence-transformers) and ``gemini`` have
    no usable list endpoint — the frontend falls back to curated
    suggestions.
    """
    _build_stub_provider(
        EmbeddingProvider,
        provider=body.provider,
        config=body.config,
        models=[{"name": "_probe"}],
    )
    if body.provider == "openai":
        return await _probe_openai_compatible_models(body.config)
    raise BadRequestError(
        f"live model discovery is not supported for embedding "
        f"provider {body.provider!r}; populate the models list "
        f"manually or use the UI's 'Suggest models' fallback.",
    )


# ---- Discovery probes ------------------------------------------------------


async def _probe_ollama_models(config: dict[str, Any]) -> dict:
    """List models locally available on an Ollama server."""
    import ollama

    headers: dict[str, str] = {}
    api_key = config.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client = ollama.AsyncClient(host=config["url"], headers=headers or None)
    try:
        resp = await client.list()
    except Exception as exc:  # pragma: no cover — network paths
        raise BadRequestError(
            f"ollama probe failed: {type(exc).__name__}: {exc}",
        ) from exc
    finally:
        # ollama.AsyncClient does not expose aclose() in all versions;
        # rely on httpx's GC for cleanup.
        pass
    # ollama.list() returns a ListResponse with .models[].model (the name).
    # context_length is not exposed on the list endpoint; would need a
    # per-model `client.show(name)` call. Skip — operators edit it in
    # the form.
    models = getattr(resp, "models", None) or resp.get("models", [])
    out: list[dict[str, Any]] = []
    for m in models:
        name = getattr(m, "model", None) or (m.get("model") if isinstance(m, dict) else None)
        if name:
            out.append({"name": name})
    return {"models": out}


async def _probe_openai_compatible_models(config: dict[str, Any]) -> dict:
    """List models from an OpenAI-compatible /v1/models endpoint."""
    import httpx

    url = str(config["url"]).rstrip("/")
    api_key = config.get("api_key") or ""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{url}/models", headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # pragma: no cover — network paths
        raise BadRequestError(
            f"openai-compatible probe failed: {type(exc).__name__}: {exc}",
        ) from exc
    items = data.get("data") or []
    # OpenAI returns {data: [{id, ...}, ...]}. context_length isn't in
    # the list response (it's model-specific; the chat-completions API
    # returns it on a per-request error path). The UI seeds a default
    # context_length and the operator can edit it.
    return {"models": [{"name": m["id"]} for m in items if "id" in m]}


# ---- CrossEncoderProvider router -------------------------------------------

cross_encoder_provider_router = make_crud_router(
    model_cls=CrossEncoderProvider,
    storage_dep=get_cross_encoder_provider_storage,
    plural="cross_encoder_providers",
    tag="cross-encoder-providers",
    on_update=_invalidate_cross_encoder,
    on_delete=_invalidate_cross_encoder,
    on_pre_create=_make_reserved_create_guard(RESERVED_CROSS_ENCODER_IDS, "cross_encoder_provider"),
    on_pre_delete_id=_make_reserved_delete_guard(RESERVED_CROSS_ENCODER_IDS, "cross_encoder_provider"),
)


@cross_encoder_provider_router.post(
    "/cross_encoder_providers/{provider_id}/invalidate",
    status_code=204,
    summary="Invalidate cached cross-encoder adapter",
    responses=common_responses(500),
)
async def invalidate_cross_encoder_provider(
    provider_id: str = Path(..., description="CrossEncoderProvider id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> None:
    await registry.invalidate_cross_encoder(provider_id)


@cross_encoder_provider_router.get(
    "/cross_encoder_providers/{provider_id}/models",
    summary="Fetch live model list from the cross-encoder provider",
    responses=common_responses(404, 500, 502, 504),
)
async def get_cross_encoder_provider_models(
    provider_id: str = Path(..., description="CrossEncoderProvider id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> dict:
    adapter = await registry.get_cross_encoder(provider_id)
    models = await adapter.list_models()
    return {"models": list(models)}


# Register Toolset in the CDC kinds registry so the harness service can
# resolve it via known_cdc_kinds().  Toolset is harness-managed but has no
# internal-collections vector index entry, so no CDC event hooks are wired.
register_cdc_kind("toolset", Toolset)


# ---- Toolset storage helper (for reference check) --------------------------


def _get_tool_approval_policy_storage(request: Request):
    return request.app.state.storage_provider.get_storage(ToolApprovalPolicy)


# ---- Toolset create connectivity probe -------------------------------------
#
# Before persisting an MCP toolset that talks to a remote endpoint over the
# network (transport=http), we drain a live tools/list to confirm the endpoint
# can be REACHED. The reject means strictly "the backend could not connect" --
# a connection refusal / DNS failure / network error, or the probe timing out.
# It is rejected BEFORE storage.create so the operator gets an immediate,
# actionable error instead of a blind save.
#
# A server that RESPONDS at all is reachable and is allowed through, even if it
# responds with an error: OAuth-protected servers (401 -> AuthenticationError),
# needs-consent (AuthRequiredError), 5xx (ProviderError/ServerError), or a
# ConfigError are all "reachable" -- an OAuth toolset in particular MUST be
# creatable so the user can then consent. The post-create TS_ConnectResult
# probe + the T0711 banner surface those reachable-but-erroring toolsets after
# creation, so nothing is lost by allowing them.
#
# Error contract (shared verbatim with ui/components/toolsets.jsx): the reject
# is a ToolsetUnreachableError -> HTTP 400 + problem type
# "/errors/toolset-unreachable". The Console matches on that `type` to render
# the inline error plus a "Create anyway" button that re-POSTs with
# ?allow_unreachable=true (which skips this probe).


def _toolset_probe_bypassed(request: Request) -> bool:
    """Whether the caller opted out of the probe via ``?allow_unreachable``.

    This is the "Create anyway" escape hatch: create the row regardless of
    reachability. Accepts ``1`` / ``true`` / ``yes`` (case-insensitive).
    """
    raw = request.query_params.get("allow_unreachable")
    return str(raw).strip().lower() in ("1", "true", "yes")


def _toolset_unreachable(
    entity: Toolset, cause: BaseException, *, timed_out: bool = False
) -> ToolsetUnreachableError:
    """Build the single reject error for a failed connectivity probe."""
    if timed_out:
        why = "connection timed out after 8s"
    elif isinstance(cause, PrimerError):
        why = cause.message
    else:
        why = f"{type(cause).__name__}: {cause}"
    return ToolsetUnreachableError(
        f"Could not connect to the MCP endpoint for toolset {entity.id!r}: "
        f"{why}. Fix the URL/headers, or create it anyway to save it despite "
        "being unreachable.",
        cause=cause if isinstance(cause, Exception) else None,
    )


# Per-toolset bound for the /v1/tools catalogue.
#
# The create-time probe above can afford 8s: it is one toolset, on an explicit
# operator action, and the operator is watching. The catalogue enumerates EVERY
# toolset on page load, so the same bound would be paid per row.
#
# It only has to be generous enough for a healthy MCP server's connect +
# handshake + tools/list, which is well under a second on a LAN.
_CATALOGUE_PROBE_TIMEOUT_S = 5.0


async def _catalogue_tools(
    registry: Any, tid: str, principal: Any,
) -> tuple[list[dict], str | None]:
    """Drain one toolset's tools under a time bound.

    Returns ``(tools, unavailable_reason)`` -- the reason is ``None`` when the
    toolset answered.

    The bound is the point. Catching exceptions is not enough to keep one bad
    toolset from taking down the catalogue: an MCP server that accepts the TCP
    connection and then never replies raises nothing at all, so an
    ``except`` clause never runs and the request hangs forever. That is not
    hypothetical -- it is what a Deployment whose pod is Running but whose
    process is wedged looks like from the client side.
    """
    import asyncio

    tools: list[dict] = []
    try:
        async with asyncio.timeout(_CATALOGUE_PROBE_TIMEOUT_S):
            provider = await registry.get_toolset(tid)
            async for tool in provider.list_tools(principal=principal):
                tools.append({
                    "id": tool.id,
                    "scoped_id": f"{tid}__{tool.id}",
                    "description": tool.description or "",
                    "input_schema": tool.args_schema or {},
                })
    except Exception as exc:  # noqa: BLE001
        # HTTP MCP runs in anyio task groups, so the useful error is a leaf of
        # a BaseExceptionGroup; reporting the group renders an unreadable
        # "unhandled errors in a TaskGroup (1 sub-exception)" in the console.
        leaf = _informative_leaf(exc)
        if isinstance(leaf, TimeoutError):
            return [], (
                f"did not respond within {_CATALOGUE_PROBE_TIMEOUT_S:g}s"
            )
        return [], f"{type(leaf).__name__}: {leaf}"
    return tools, None


def _informative_leaf(exc: BaseException) -> BaseException:
    """Unwrap anyio ``BaseExceptionGroup`` wrappers to the informative leaf.

    HTTP MCP runs inside anyio task groups, which wrap any sub-exception in a
    ``BaseExceptionGroup``. Prefer an already-classified :class:`PrimerError`
    leaf (what :class:`McpToolsetProvider` raises for connection failures via
    ``classify_mcp_exception``), exactly like :func:`list_toolset_tools` above;
    otherwise fall back to the first leaf.
    """
    if not isinstance(exc, BaseExceptionGroup):
        return exc
    leaves: list[BaseException] = []
    pending: list[BaseException] = [exc]
    while pending:
        cur = pending.pop()
        if isinstance(cur, BaseExceptionGroup):
            pending.extend(cur.exceptions)
        else:
            leaves.append(cur)
    for leaf in leaves:
        if isinstance(leaf, PrimerError):
            return leaf
    return leaves[0] if leaves else exc


def _is_connection_failure(leaf: BaseException) -> bool:
    """Pure decision: does this probe outcome mean "could not connect"?

    Only a genuine transport-level failure counts as unreachable -> reject:

    * :class:`NetworkError` -- connection refused / DNS failure / network error
      (no response received), and
    * :class:`TimeoutError` -- the 8s probe cap fired before the handshake
      completed.

    Every "the server responded" outcome is REACHABLE -> allow the create:
    :class:`AuthenticationError` (401/403), :class:`AuthRequiredError`
    (needs OAuth consent), :class:`ProviderError` / :class:`ServerError`
    (5xx from a responding server), :class:`ConfigError`, or anything else.
    """
    return isinstance(leaf, (NetworkError, TimeoutError))


async def _probe_mcp_reachable(entity: Toolset, request: Request) -> None:
    """Fully drain ``list_tools`` to confirm the http MCP endpoint is reachable.

    Builds a transient :class:`McpToolsetProvider` (no persistence, no
    registry) and consumes the whole ``tools/list`` under an 8s cap. Rejects
    the create with :class:`ToolsetUnreachableError` ONLY when the endpoint
    could not be connected to (:func:`_is_connection_failure`); any outcome
    where the server responded (auth / provider / config / other) is allowed
    through -- the post-create probe surfaces those errors.
    """
    import asyncio

    from primer.toolset.mcp import McpToolsetProvider

    principal = getattr(request.state, "principal", None)
    # Transient provider: this path is only reached for http transport, so the
    # stdio allowlist is irrelevant (None). McpToolsetProvider.list_tools opens
    # and closes its own short-lived session per call, so nothing leaks.
    provider = McpToolsetProvider(
        toolset_id=entity.id,
        config=entity.config,  # type: ignore[arg-type]  # http => McpConfig set
        allowed_stdio_commands=None,
    )
    try:
        async with asyncio.timeout(8):
            async for _tool in provider.list_tools(principal=principal):
                pass  # drain fully: connect + handshake + tools/list happen here
    except Exception as exc:
        # asyncio.timeout raises TimeoutError; the mcp transport raises a
        # classified PrimerError (often wrapped in a BaseExceptionGroup).
        leaf = _informative_leaf(exc)
        if _is_connection_failure(leaf):
            raise _toolset_unreachable(
                entity, leaf, timed_out=isinstance(leaf, TimeoutError)
            ) from exc
        # Reachable but errored (auth / provider / config / other): allow the
        # create. The post-create TS_ConnectResult probe surfaces the error.
        logger.debug(
            "toolset %r create probe: endpoint reachable but errored (%s); "
            "allowing create",
            entity.id,
            type(leaf).__name__,
        )
        return


async def _toolset_on_pre_create(entity: Toolset, request: Request) -> None:
    """Reject creating a network MCP toolset whose endpoint is unreachable.

    Runs before ``storage.create`` (raising aborts the create, persisting
    nothing). Only network MCP transports are probed -- both ``http``
    (streamable) and ``sse`` (legacy); the ``allow_unreachable`` bypass,
    non-MCP toolsets, and stdio MCP (no remote endpoint -- probing it would
    launch a subprocess) all skip the probe.

    Also rejects the reserved ``external`` toolset id (409): that scope
    is claimed by invoker-supplied per-invocation tools
    (primer.agent.external_tools), so a stored toolset must never
    collide with it. Checked before the probe bypass on purpose.
    """
    if entity.id in RESERVED_TOOLSET_IDS:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "reserved_id",
                "kind": "toolset",
                "reserved": sorted(RESERVED_TOOLSET_IDS),
                "message": (
                    f"id {entity.id!r} is reserved and cannot be "
                    "created via the API"
                ),
            },
        )
    if _toolset_probe_bypassed(request):
        return
    if entity.provider == ToolsetProviderType.PYTHON:
        _validate_python_toolset(entity)
        return
    if entity.provider != ToolsetProviderType.MCP:
        return
    config = entity.config
    if config is None or config.transport not in (
        TransportType.HTTP,
        TransportType.SSE,
    ):
        return
    await _probe_mcp_reachable(entity, request)


def _validate_python_toolset(entity: Toolset) -> None:
    """Reject a python toolset whose source cannot produce tools.

    Registration is where a bad tool must fail. Calling time is too late: an
    agent mid-turn should never be the thing that discovers a docstring is
    missing. The RFC7807 extensions carry ``field`` and ``lineno`` so the
    console can point at the offending line.
    """
    from primer.toolset.python_runner.registration import (
        RegistrationError,
        register_module,
    )

    config = entity.config
    try:
        register_module(
            config.source, entity.id, config.default_timeout_seconds
        )
    except RegistrationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_python_toolset",
                "message": str(exc),
                "field": exc.field,
                "lineno": exc.lineno,
            },
        ) from exc


async def _toolset_on_pre_update(
    entity: Toolset, existing: Toolset, request: Request
) -> None:
    """Validate python source and own ``source_version`` server-side.

    The client's version is advisory. If two operators edit concurrently they
    both send the version they read, and a parked resume could not tell which
    code it was about to run. The server bumps instead, so the number always
    moves when the source does.
    """
    if entity.provider != ToolsetProviderType.PYTHON:
        return
    _validate_python_toolset(entity)
    prior = existing.config if existing is not None else None
    if prior is not None and getattr(prior, "source", None) == entity.config.source:
        entity.config.source_version = prior.source_version
    elif prior is not None:
        entity.config.source_version = prior.source_version + 1


# ---- Toolset router --------------------------------------------------------

toolset_router = make_crud_router(
    model_cls=Toolset,
    storage_dep=get_toolset_storage,
    plural="toolsets",
    tag="toolsets",
    on_update=_invalidate_toolset,
    on_delete=_invalidate_toolset,
    on_pre_create=_toolset_on_pre_create,
    on_pre_update=_toolset_on_pre_update,
    managed_by_field="harness_id",
    references=[
        ReferenceCheck(
            child_kind="tool_approval_policy",
            child_storage=_get_tool_approval_policy_storage,
            child_field="toolset_id",
        ),
    ],
)


@toolset_router.post(
    "/toolsets/{toolset_id}/invalidate",
    status_code=204,
    summary="Invalidate cached toolset provider",
    responses=common_responses(500),
)
async def invalidate_toolset_provider(
    toolset_id: str = Path(..., description="Toolset id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> None:
    await registry.invalidate_toolset(toolset_id)


@toolset_router.get(
    "/toolsets/{toolset_id}/tools",
    summary="List tools currently exposed by a toolset",
    responses=common_responses(401, 404, 500, 502, 504),
)
async def list_toolset_tools(
    toolset_id: str = Path(..., description="Toolset id"),
    principal: str | None = Depends(get_principal),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> dict:
    """Enumerate the toolset's tools from the live provider.

    OAuth-protected MCP toolsets raise ``AuthRequiredError``, which the
    error mapper serialises as 401 + ``extensions.auth_url`` so the
    caller can prompt the user to consent.
    """
    from primer.model.except_ import ProviderError

    provider = await registry.get_toolset(toolset_id)
    tools = []
    try:
        async for tool in provider.list_tools(principal=principal):
            tools.append(tool.model_dump(mode="json"))
    except BaseExceptionGroup as group:
        # HTTP/SSE MCP transports run inside anyio task groups, which
        # wrap any sub-exception in BaseExceptionGroup. Unwrap to find
        # the most informative inner exception, then map to the same
        # ProviderError envelope as the plain-Exception path.
        from primer.model.except_ import PrimerError
        # Surface a documented PrimerError if any sub-exception is one
        for sub in group.exceptions:
            if isinstance(sub, PrimerError):
                raise sub from group
        # Otherwise, take the first sub-exception's type+message
        first = group.exceptions[0] if group.exceptions else group
        raise ProviderError(
            f"toolset {toolset_id!r} provider failed to enumerate tools: "
            f"{type(first).__name__}: {first}"
        ) from group
    except Exception as exc:
        # Re-raise the documented primer error types so the registry
        # mapper produces the correct envelope (NotFoundError → 404,
        # AuthRequiredError → 401, etc.).
        from primer.model.except_ import PrimerError
        if isinstance(exc, PrimerError):
            raise
        # MCP stdio transport failures (handshake refused, connection
        # closed, subprocess crash) come back as third-party exception
        # types like mcp.shared.exceptions.McpError. Map to 502
        # provider-error rather than letting the 500 leak.
        raise ProviderError(
            f"toolset {toolset_id!r} provider failed to enumerate tools: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return {"tools": tools}


# ---------- Built-in toolsets registry --------------------------------------
#
# The five reserved built-in toolset ids live in
# primer/api/registries/provider_registry.py. Their operator-facing
# metadata (tagline, icon hint, availability semantics) lives here so
# the UI can render the built-in cards dynamically from one source of
# truth instead of hard-coding the list.

_BUILTIN_TOOLSETS: list[dict] = [
    {
        "id": "system",
        "tagline": "Operator + diagnostic tools",
        "icon": "settings",
        "always_on": True,
    },
    {
        "id": "workspaces",
        "tagline": "File ops + exec inside the bound workspace",
        "icon": "box",
        "always_on": True,
    },
    {
        "id": "search",
        "tagline": (
            "Semantic search over indexed entities — available once "
            "Internal Collections is bootstrapped"
        ),
        "icon": "search",
        "always_on": False,  # gated on IC subsystem
    },
    {
        "id": "misc",
        "tagline": "Datetime, sleep, UUID, hashing, arithmetic utilities",
        "icon": "wrench",
        "always_on": True,
    },
    {
        "id": "web",
        "tagline": "DuckDuckGo search + page-fetch primitives",
        "icon": "external",
        "always_on": True,
    },
    {
        "id": "harness",
        "tagline": "Harness lifecycle: register, fetch, install, sync, uninstall",
        "icon": "package",
        "always_on": True,
    },
    {
        "id": "trigger",
        "tagline": "Trigger + subscription CRUD and the subscribe_to_trigger yield",
        "icon": "clock",
        "always_on": True,
    },
]

# A dedicated router registered BEFORE toolset_router in app.py so that
# GET /toolsets/builtin is matched by this literal route rather than
# being captured as toolset_id="builtin" by the CRUD GET-by-id.
builtin_toolsets_router = APIRouter()


@builtin_toolsets_router.get(
    "/toolsets/builtin",
    summary="List the five built-in toolsets and their availability",
)
async def list_builtin_toolsets(
    storage_provider=Depends(get_storage_provider),
) -> dict:
    """Operator-facing catalogue of the always-available built-ins.

    The UI uses this in place of a hard-coded list so adding a new
    built-in toolset to the runtime doesn't require a UI change.
    ``available`` is True for always-on built-ins; for the IC-gated
    ``search`` toolset, it's True iff an InternalCollectionsConfig
    row exists in storage (mirrors the topbar IC-config probe).
    """
    from primer.model.internal import InternalCollectionsConfig

    # Probe IC config to decide search-toolset availability.
    ic_storage = storage_provider.get_storage(InternalCollectionsConfig)
    ic_active = False
    try:
        page = await ic_storage.find(
            None, OffsetPage(offset=0, length=1),
        )
        ic_active = len(page.items) > 0
    except Exception:
        # Storage layer failure: treat as IC-off rather than 500.
        ic_active = False

    items = []
    for spec in _BUILTIN_TOOLSETS:
        available = spec["always_on"] or (
            spec["id"] == "search" and ic_active
        )
        items.append({
            **spec,
            "available": available,
        })
    return {"items": items}


@builtin_toolsets_router.get(
    "/toolsets/{toolset_id}",
    summary="Get a toolset (built-in synthesised, user-defined from storage)",
    responses=common_responses(404, 500),
)
async def get_toolset_with_builtin_shim(
    toolset_id: str = Path(..., description="Toolset id"),
    storage_provider=Depends(get_storage_provider),
) -> dict:
    """GET /v1/toolsets/{id}.

    Reserved (built-in) toolsets don't have rows in the ``Toolset``
    storage backend — they're singletons on the ProviderRegistry — so
    a naive CRUD .get() returns 404 for them, breaking the console's
    detail page. This shim synthesises a Toolset-shaped response for
    every id in ``RESERVED_TOOLSET_IDS`` and falls back to the
    storage row for everything else.

    Registered on ``builtin_toolsets_router`` which is included BEFORE
    the CRUD-generated ``toolset_router`` in ``primer/api/app.py``, so
    this route shadows the CRUD GET-by-id. User-defined ids fall
    through here too (we delegate to storage), so the CRUD route is
    effectively never hit for GET-by-id; that's intentional.
    """
    from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
    from primer.model.provider import Toolset

    if toolset_id in RESERVED_TOOLSET_IDS:
        spec = next(
            (s for s in _BUILTIN_TOOLSETS if s["id"] == toolset_id),
            None,
        )
        tagline = spec["tagline"] if spec else f"Built-in {toolset_id!r} toolset."
        return {
            "id": toolset_id,
            "provider": "internal",
            "config": None,
            "description": tagline,
            "tagline": tagline,
            "icon": spec["icon"] if spec else "box",
            "builtin": True,
            "harness_id": None,
        }

    row = await storage_provider.get_storage(Toolset).get(toolset_id)
    if row is None:
        from primer.model.except_ import NotFoundError
        raise NotFoundError(
            f"Toolset {toolset_id!r} does not exist"
        )
    body = row.model_dump(mode="json")
    body["builtin"] = False
    return body


@builtin_toolsets_router.get(
    "/tools",
    summary=(
        "List every tool currently exposed by every registered "
        "toolset (built-in + user-defined)."
    ),
)
async def list_all_tools(
    principal: str | None = Depends(get_principal),
    registry: ProviderRegistry = Depends(get_provider_registry),
    storage_provider=Depends(get_storage_provider),
) -> dict:
    """Fan out across every reachable toolset and return a single
    flat catalogue keyed by toolset.

    Powers the operator console's per-tool agent picker — without
    this, the UI would have to issue one ``/toolsets/{id}/tools``
    request per toolset to populate the search box.

    Failure tolerance: a toolset that 401s, 5xxs, or just times out
    is reported with ``available: false`` and the failure reason
    instead of bringing down the whole catalogue. The UI shows the
    rest of the picker so one broken MCP server doesn't block the
    operator from configuring an agent.
    """
    from primer.model.internal import InternalCollectionsConfig
    from primer.model.provider import Toolset

    out: list[dict] = []

    # 1. Built-in toolsets (system / workspaces / search / misc / web).
    # ``search`` is gated by the InternalCollectionsConfig probe — same
    # logic as list_builtin_toolsets() above.
    ic_storage = storage_provider.get_storage(InternalCollectionsConfig)
    ic_active = False
    try:
        page = await ic_storage.find(
            None, OffsetPage(offset=0, length=1),
        )
        ic_active = len(page.items) > 0
    except Exception:
        ic_active = False

    for spec in _BUILTIN_TOOLSETS:
        tid = spec["id"]
        available = spec["always_on"] or (tid == "search" and ic_active)
        entry: dict = {
            "id": tid,
            "builtin": True,
            "label": spec.get("label", tid),
            "tagline": spec.get("tagline", ""),
            "available": available,
            "tools": [],
        }
        if not available:
            entry["unavailable_reason"] = (
                "internal collections not bootstrapped"
                if tid == "search" else "subsystem disabled"
            )
            out.append(entry)
            continue
        tools, reason = await _catalogue_tools(registry, tid, principal)
        if reason is None:
            entry["tools"] = tools
        else:
            entry["available"] = False
            entry["unavailable_reason"] = reason
        out.append(entry)

    # 2. User-defined Toolset rows. Page through storage so the
    # catalogue scales beyond the default 200-row cap.
    ts_storage = storage_provider.get_storage(Toolset)
    seen_user_ids: set[str] = set()
    offset = 0
    page_size = 200
    while True:
        page = await ts_storage.list(
            OffsetPage(offset=offset, length=page_size),
        )
        for row in page.items:
            if row.id in seen_user_ids:
                continue
            seen_user_ids.add(row.id)
            entry = {
                "id": row.id,
                "builtin": False,
                "label": row.id,
                # Toolset rows inherit Identifiable (no `description`
                # field); use a getattr-guard so we don't 500 when the
                # row is built-in-style (id only).
                "tagline": getattr(row, "description", "") or "",
                "available": True,
                "tools": [],
            }
            tools, reason = await _catalogue_tools(
                registry, row.id, principal,
            )
            if reason is None:
                entry["tools"] = tools
            else:
                entry["available"] = False
                entry["unavailable_reason"] = reason
            out.append(entry)
        if len(page.items) < page_size:
            break
        offset += page_size

    return {"items": out}


__all__ = [
    "builtin_toolsets_router",
    "cross_encoder_provider_router",
    "embedding_provider_router",
    "llm_provider_router",
    "toolset_router",
]


class ValidatePythonSourceBody(BaseModel):
    """Candidate source, not yet saved."""

    source: str = Field(
        ..., description="Python module source to analyse without persisting.",
    )


@toolset_router.post(
    "/toolsets/{toolset_id}/validate",
    summary="Dry-run: what would this source register, and what is wrong with it",
    responses=common_responses(404, 500),
)
async def validate_python_source(
    toolset_id: str = Path(..., description="Toolset id"),
    body: ValidatePythonSourceBody = Body(...),
    storage_provider=Depends(get_storage_provider),
) -> dict:
    """Analyse candidate source WITHOUT saving it.

    The editor needs the registrar's verdict while the operator is still
    typing. Until this existed the only way to learn that a docstring was
    malformed was to save, which meant every mistake round-tripped through a
    write to the stored toolset.

    Always 200: a source that cannot register is the normal case here, not an
    HTTP error. ``ok`` distinguishes them. (The PUT path still 422s -- there,
    invalid source is a rejected write.)

    Safe by construction: :func:`register_module` walks the AST and never
    imports or executes the module, which is the same property that lets the
    save path validate untrusted source.
    """
    from primer.model.providers.toolset import PythonConfig
    from primer.toolset.python_runner.registration import (
        RegistrationError,
        register_module,
    )

    # The per-tool default only affects reported timeouts, so a missing or
    # non-python toolset is not worth a 404 here -- fall back and analyse.
    default_timeout = 30.0
    storage = storage_provider.get_storage(Toolset)
    row = await storage.get(toolset_id)
    if row is not None and isinstance(row.config, PythonConfig):
        default_timeout = row.config.default_timeout_seconds

    try:
        registered = register_module(body.source, toolset_id, default_timeout)
    except RegistrationError as exc:
        return {
            "ok": False,
            "tools": [],
            "error": {
                "message": str(exc),
                "field": exc.field,
                "lineno": exc.lineno,
            },
        }

    return {
        "ok": True,
        "error": None,
        "tools": [
            {
                "id": reg.tool.id,
                "fn_name": reg.fn_name,
                "yields": reg.tool.yields,
                "timeout_seconds": reg.timeout_seconds,
                "description": reg.tool.description or "",
                "args": sorted(
                    (reg.tool.args_schema or {}).get("properties", {}).keys()
                ),
                # Where the function sits, so the outline can jump to it.
                "lineno": reg.lineno,
            }
            for reg in registered
        ],
    }


@toolset_router.get(
    "/toolsets/{toolset_id}/runtime",
    summary="Runtime facts about a toolset: isolation level and derived tools",
    responses=common_responses(404, 500),
)
async def toolset_runtime(
    toolset_id: str = Path(..., description="Toolset id"),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> dict:
    """What a python toolset actually is at runtime.

    ``isolation_level`` is what this deployment ENFORCES, not what the
    strongest backend could: ``rlimit-only`` bounds CPU and memory but stops
    neither filesystem reads nor egress, and the console shows that rather
    than a generic "sandboxed" badge.
    """
    provider = await registry.get_toolset(toolset_id)
    # `yields` is not part of Tool's serialisation, but it is the single most
    # important thing to know about a python tool at a glance: a yielding tool
    # parks the run rather than returning. The console renders a badge from it,
    # so add it explicitly rather than leaving the badge unreachable.
    tools = []
    async for t in provider.list_tools():
        entry = t.model_dump(mode="json")
        entry["yields"] = bool(getattr(t, "yields", False))
        tools.append(entry)
    error = getattr(provider, "registration_error", None)
    return {
        "toolset_id": toolset_id,
        "isolation_level": getattr(provider, "isolation_level", None),
        "tools": tools,
        "registration_error": (
            {
                "message": str(error),
                "field": error.field,
                "lineno": error.lineno,
            }
            if error is not None
            else None
        ),
    }
