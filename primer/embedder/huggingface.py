"""HuggingFace embedder adapter — wraps sentence-transformers locally.

Subclasses :class:`primer.int.Embedder`. First primer adapter that is
not API-shaped: no SDK client, no HTTP. Wraps the synchronous
:class:`sentence_transformers.SentenceTransformer` in
:func:`asyncio.to_thread`.

Models load lazily on first :meth:`embed` call against each model
(downloads from HuggingFace Hub on first use, cached locally
thereafter via the standard HF cache).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from sentence_transformers import SentenceTransformer

from primer.int.coordinator import RateLimiter
from primer.int.embedder import Embedder
from primer.model.chat import (
    AudioPart,
    DocumentPart,
    ImagePart,
    VideoPart,
)
from primer.model.embedding import (
    EmbedResponse,
    Embedding,
    EmbeddingPart,
    ExtendedEmbeddingConfig,
    ExtendedEmbeddingMetadata,
    ExtendedEmbeddingPart,
    PerTokenVectors,
    TextPart,
    TokensPart,
)
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    ConfigError,
    PrimerError,
    ModelNotFoundError,
    NetworkError,
    ProviderError,
    UnsupportedContentError,
)
from primer.model.provider import (
    EmbeddingProvider,
    EmbeddingProviderType,
    HuggingFaceConfig,
)


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Pure helpers                                                                 #
# --------------------------------------------------------------------------- #


def _part_to_text(part: EmbeddingPart) -> str:
    """Translate one universal :class:`EmbeddingPart` into a plain
    string for the model encoder. HuggingFace text adapter is
    text-only — every other modality raises."""
    if isinstance(part, TextPart):
        return part.text
    if isinstance(part, ImagePart):
        raise UnsupportedContentError(
            "HuggingFace text embedder is text-only; got image"
        )
    if isinstance(part, ExtendedEmbeddingPart):
        ext = part.extended
        if isinstance(ext, TokensPart):
            raise UnsupportedContentError(
                "pre-tokenised input not supported; pass text"
            )
        type_name = type(ext).__name__
        if "Audio" in type_name:
            raise UnsupportedContentError("audio not supported by text embedder")
        if "Video" in type_name:
            raise UnsupportedContentError("video not supported by text embedder")
        if "Document" in type_name:
            raise UnsupportedContentError(
                "document not supported by text embedder; pre-extract text"
            )
        raise UnsupportedContentError(  # pragma: no cover
            f"unsupported extended type {type_name}"
        )
    raise UnsupportedContentError(  # pragma: no cover
        f"unexpected part type {type(part).__name__}"
    )


def _encode_sync(
    model, texts: list[str], output_value: str, prompt: str | None = None,
):
    """Sync wrapper around SentenceTransformer.encode — runs in
    asyncio.to_thread.

    ``normalize_embeddings`` is True so the produced vectors land on
    the unit hypersphere. Every vector store we ship (LanceDB,
    pgvector) ranks by cosine similarity, which is only well-defined
    after L2 normalisation — without it short queries (e.g. "web
    search") would land far from long passages ("web-search: Perform
    a web search and return…") simply because of magnitude, not
    semantics. Affects every model SentenceTransformer wraps,
    including BGE / E5 / GTE / MiniLM. Operators who relied on raw
    magnitudes for custom rerank will need to scale their thresholds.

    ``prompt`` is the model-family-specific instruction prepended to
    each text. Asymmetric-retrieval models (BGE, E5, nomic-embed-text)
    were trained to expect a different prompt on queries vs documents;
    without it, the query embedding lands in a slightly different
    region of vector space and similarity scores collapse — a "web
    search" query against a "web-search: Perform a web search…"
    document drops from ~0.7 to ~0.25 cosine on bge-small-en-v1.5
    without the query prompt. The caller picks the prompt via
    ``_query_prompt_for_model`` / ``_document_prompt_for_model``.
    """
    if prompt:
        texts = [prompt + t for t in texts]
    return model.encode(
        texts,
        output_value=output_value,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )


# Mapping of model-family substring → (query_prompt, document_prompt).
# Both can be None when the family doesn't recommend a prefix. Match
# is by case-insensitive substring on the SentenceTransformer model id.
# Operators who use a model not on this list (or who want to override
# the defaults) can pass ``config.raw["query_prompt"]`` /
# ``config.raw["document_prompt"]`` to bypass.
_MODEL_FAMILY_PROMPTS: list[tuple[str, str | None, str | None]] = [
    # BGE: asymmetric. Query gets the prompt; document does not.
    # Source: model card (BAAI/bge-small-en-v1.5, bge-large, bge-m3).
    ("bge", "Represent this sentence for searching relevant passages: ", None),
    # E5: symmetric prefixes. Both sides need a prefix to be in-distribution.
    # Source: intfloat/e5-* / multilingual-e5-* model cards.
    ("e5", "query: ", "passage: "),
    # nomic-embed-text: symmetric task-prefixed.
    # Source: nomic-ai/nomic-embed-text-v1* / v1.5 model card.
    ("nomic-embed-text", "search_query: ", "search_document: "),
]


def _resolve_prompts_for_model(model_name: str) -> tuple[str | None, str | None]:
    """Return (query_prompt, document_prompt) for a model.

    Unknown families get (None, None) — encode raw text on both sides,
    matching the default SentenceTransformer behaviour.
    """
    lower = model_name.lower()
    for needle, qp, dp in _MODEL_FAMILY_PROMPTS:
        if needle in lower:
            return qp, dp
    return None, None


def _select_prompt(
    *, task_type: str | None, model_name: str, raw: dict
) -> str | None:
    """Pick the prompt prefix for this call.

    Precedence: explicit ``raw["query_prompt"]`` / ``raw["document_prompt"]``
    override family defaults, so an operator can use a non-default
    prompt for a model we don't recognise. Without a ``task_type`` hint
    we treat the input as a document (the conservative choice — only
    the search code path opts into ``retrieval_query``).
    """
    if task_type == "retrieval_query":
        if "query_prompt" in raw:
            return raw["query_prompt"] or None
        return _resolve_prompts_for_model(model_name)[0]
    # Default to document semantics for everything else (None task_type,
    # retrieval_document, semantic_similarity, classification, ...). Only
    # E5 / nomic actually prefix documents; BGE / MiniLM / GTE do not.
    if "document_prompt" in raw:
        return raw["document_prompt"] or None
    return _resolve_prompts_for_model(model_name)[1]


def _translate_response(
    model: str,
    arrays: Any,
    output_value: str,
    output_dimensions: int | None,
) -> EmbedResponse:
    """Translate the encoder's numpy output into a universal :class:`EmbedResponse`."""
    embeddings: list[Embedding] = []
    for i, arr in enumerate(arrays):
        if output_value == "token_embeddings":
            vectors_2d = arr.tolist()
            extended = ExtendedEmbeddingMetadata(
                per_token_vectors=PerTokenVectors(vectors=vectors_2d)
            )
            mean_vec = arr.mean(axis=0).tolist()
            if output_dimensions is not None:
                mean_vec = mean_vec[:output_dimensions]
            embeddings.append(Embedding(index=i, vector=mean_vec, extended=extended))
        else:
            vec = arr.tolist()
            if output_dimensions is not None:
                vec = vec[:output_dimensions]
            embeddings.append(Embedding(index=i, vector=vec, extended=None))
    return EmbedResponse(model=model, embeddings=embeddings, usage=None)


def _classify_hf_exception(exc: Exception) -> PrimerError:
    """Inline classifier — sentence-transformers / huggingface_hub
    don't share a common base exception class, so we string-match on
    common error patterns plus check OSError for I/O failures."""
    msg = str(exc)
    type_name = type(exc).__name__
    if (
        "401" in msg
        or "GatedRepoError" in type_name
        or "authentication" in msg.lower()
    ):
        return AuthenticationError(
            "HuggingFace Hub authentication failed",
            cause=exc,
        )
    if "404" in msg or "RepositoryNotFoundError" in type_name:
        return BadRequestError(
            f"HuggingFace model not found: {msg}",
            cause=exc,
        )
    if isinstance(exc, OSError):
        return NetworkError(
            f"HuggingFace I/O failure: {type_name}",
            cause=exc,
        )
    return ProviderError(str(exc), cause=exc)


class HuggingFaceEmbedder(Embedder):
    """Local embedding adapter via sentence-transformers."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if provider.provider != EmbeddingProviderType.HUGGINGFACE:
            raise ConfigError(
                f"HuggingFaceEmbedder requires provider type HUGGINGFACE; "
                f"got {provider.provider}"
            )
        if not isinstance(provider.config, HuggingFaceConfig):
            raise ConfigError(
                "HuggingFaceEmbedder requires HuggingFaceConfig in provider.config"
            )
        self._provider = provider
        self._config: HuggingFaceConfig = provider.config
        self._models: dict[str, SentenceTransformer] = {}
        if rate_limiter is None:
            from primer.coordinator.in_memory import InMemoryRateLimiter
            rate_limiter = InMemoryRateLimiter()
        self._rate_limiter = rate_limiter
        self._rate_limit_key = f"embedder:{provider.id}"
        self._max_concurrency = provider.limits.max_concurrency

        logger.info(
            "HuggingFace embedder initialized",
            extra={
                "provider_id": provider.id,
                "models": [m.name for m in provider.models],
                "max_concurrency": provider.limits.max_concurrency,
            },
        )

    async def list_models(self) -> Iterable[str]:
        return [m.name for m in self._provider.models]

    async def _get_model(self, name: str) -> SentenceTransformer:
        if name not in self._models:
            token_value = self._config.token.get_secret_value()
            self._models[name] = await asyncio.to_thread(
                SentenceTransformer,
                name,
                token=token_value or None,
            )
        return self._models[name]

    async def embed(  # type: ignore[override]
        self,
        *,
        model: str,
        inputs: list[EmbeddingPart],
        output_dimensions: int | None = None,
        config: ExtendedEmbeddingConfig | None = None,
    ) -> EmbedResponse:
        allowed = {m.name for m in self._provider.models}
        if model not in allowed:
            raise ModelNotFoundError(
                f"model {model!r} is not configured for provider "
                f"{self._provider.id!r}; configured models: {sorted(allowed)}"
            )

        # Map all inputs to text BEFORE acquiring the semaphore — fast-fail
        # on unsupported parts without holding a permit.
        texts = [_part_to_text(p) for p in inputs]

        raw = (config.raw or {}) if config is not None else {}
        output_value = (
            "token_embeddings"
            if raw.get("output_value") == "token_embeddings"
            else "sentence_embedding"
        )
        task_type = config.task_type if config is not None else None
        prompt = _select_prompt(task_type=task_type, model_name=model, raw=raw)

        logger.info(
            "HuggingFace embed starting",
            extra={
                "provider_id": self._provider.id,
                "model": model,
                "input_count": len(inputs),
                "output_dimensions": output_dimensions,
                "output_value": output_value,
                "task_type": task_type,
                "prompt_applied": bool(prompt),
            },
        )

        async with await self._rate_limiter.acquire(
            self._rate_limit_key, max_concurrency=self._max_concurrency,
        ):
            st_model = await self._get_model(model)
            try:
                arrays = await asyncio.to_thread(
                    _encode_sync, st_model, texts, output_value, prompt
                )
            except Exception as exc:
                err = _classify_hf_exception(exc)
                logger.error(
                    "HuggingFace embed failed",
                    extra={
                        "provider_id": self._provider.id,
                        "model": model,
                        "exception": type(exc).__name__,
                    },
                )
                raise err from exc

        return _translate_response(model, arrays, output_value, output_dimensions)
